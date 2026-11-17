from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from core.config import AppConfig
from core.diagnostics import install_loop_exception_handler
from core.models import TaskSnapshot, utc_now_iso
from core.storage import read_json, update_json
from core.task_history import TaskHistory


class TaskRunner:
    def __init__(self, config: AppConfig):
        self.config = config
        self.tasks: dict[str, asyncio.Task] = {}
        self.stop_events: dict[str, asyncio.Event] = {}
        self.log = logging.getLogger("tasks")
        self.history = TaskHistory(config.tasks_file.parent)
        # Persisted 'running' records have no live task after a process restart.
        if config.tasks_file.exists():
            def _mark_interrupted(raw: Any) -> dict[str, Any]:
                items = list(raw.get("tasks", [])) if isinstance(raw, dict) else []
                changed = False
                for item in items:
                    if isinstance(item, dict) and item.get("status") == "running":
                        item["status"] = "interrupted"
                        item["detail"] = "Previous process ended before completion"
                        item["updated_at"] = utc_now_iso()
                        changed = True
                return {"tasks": items[-200:]} if changed else raw

            update_json(config.tasks_file, {"tasks": []}, _mark_interrupted, skip_unchanged=True)

    def start(
        self,
        name: str,
        coro_factory: Callable[[asyncio.Event], Awaitable[Any]],
        *,
        progress=None,
        resume_spec=None,
        checkpoint_factory=None,
        resume_from=None,
    ) -> str:
        install_loop_exception_handler()
        active = next((task for task in self.tasks.values() if not task.done()), None)
        if active is not None:
            raise RuntimeError(f"Task '{active.get_name()}' is already running. Stop it before starting '{name}'.")
        task_id = uuid.uuid4().hex[:12]
        if resume_spec is not None and checkpoint_factory is None:
            raise ValueError("Checkpointed tasks require an explicit safe executor")
        job_id = (
            self.history.resume(resume_from[0], resume_from[1], resume_spec, "qt", confirmed=True)
            if resume_from
            else self.history.create(name, "qt", resume_spec)
        )
        stop_event = asyncio.Event()
        self.stop_events[task_id] = stop_event
        try:
            self._save_snapshot(TaskSnapshot(id=task_id, name=name, status="running"))
        except BaseException as exc:
            self.stop_events.pop(task_id, None)
            self.history.finish(job_id, "failed", error=exc)
            raise

        terminal_saved = False
        pending_progress_task: asyncio.Task | None = None

        def _persist_terminal(status: str, detail: str = "", *, result: Any = None, error: BaseException | None = None) -> None:
            nonlocal terminal_saved
            try:
                self.history.finish(job_id, status, result=result, error=error)
            finally:
                self._save_snapshot(TaskSnapshot(id=task_id, name=name, status=status, detail=detail))
                terminal_saved = True

        async def wrapper() -> Any:
            nonlocal pending_progress_task
            try:
                await asyncio.to_thread(self.history.running, job_id)
                if progress:
                    previous = progress.on_update
                    last_progress_write = 0.0
                    loop = asyncio.get_running_loop()

                    def update(snapshot):
                        nonlocal last_progress_write, pending_progress_task
                        if resume_spec is None:
                            now = time.monotonic()
                            done_count = (snapshot.get("completed", 0) or 0) + (snapshot.get("errors", 0) or 0)
                            total_count = snapshot.get("total", 0) or 0
                            is_terminal_tick = total_count > 0 and done_count >= total_count
                            if (now - last_progress_write >= 0.5 or is_terminal_tick) and (
                                pending_progress_task is None or pending_progress_task.done()
                            ):
                                last_progress_write = now
                                snap_copy = dict(snapshot)

                                async def _write_progress() -> None:
                                    try:
                                        await asyncio.to_thread(self.history.progress, job_id, snap_copy)
                                    except Exception:
                                        pass

                                pending_progress_task = loop.create_task(_write_progress(), name=f"{name}-progress")
                        if previous:
                            previous(snapshot)

                    progress.on_update = update
                result = (
                    await checkpoint_factory(job_id, stop_event)
                    if checkpoint_factory
                    else await coro_factory(stop_event)
                )
            except asyncio.CancelledError:
                await asyncio.shield(asyncio.to_thread(_persist_terminal, "stopped", "Cancelled"))
                raise
            except Exception as exc:
                self.log.exception("Task %s failed", task_id)
                await asyncio.shield(asyncio.to_thread(_persist_terminal, "failed", str(exc), error=exc))
                raise
            else:
                await asyncio.shield(asyncio.to_thread(_persist_terminal, "done", result=result))
                return result

        task = asyncio.create_task(wrapper(), name=name)

        def _on_done(done_task: asyncio.Task) -> None:
            self.stop_events.pop(task_id, None)
            if not terminal_saved and done_task.cancelled():
                _persist_terminal("stopped", "Cancelled")

        task.add_done_callback(_on_done)
        self.tasks[task_id] = task
        return task_id

    async def stop(self, task_id: str) -> bool:
        stop_event = self.stop_events.get(task_id)
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.done():
            return False
        if stop_event:
            stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=8)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                await asyncio.to_thread(
                    self._save_snapshot,
                    TaskSnapshot(
                        id=task_id,
                        name=task.get_name(),
                        status="stop-timeout",
                        detail="Cleanup is still running",
                    ),
                )
                raise RuntimeError("Task cleanup did not finish; session remains busy") from None
        # wrapper owns the terminal state. Do not overwrite failed/done with
        # 'stopped' merely because completion happened during a stop request.
        return True

    async def stop_by_name_prefix(self, prefix: str) -> int:
        stopped = 0
        for task_id, task in list(self.tasks.items()):
            if task.done() or not task.get_name().startswith(prefix):
                continue
            try:
                if await self.stop(task_id):
                    stopped += 1
            except Exception as exc:
                self.log.warning("Stopping task %s (%s) raised %s: %s", task_id, task.get_name(), type(exc).__name__, exc)
        return stopped

    async def stop_all(self) -> int:
        ids = [task_id for task_id, task in list(self.tasks.items()) if not task.done()]
        for task_id in ids:
            event = self.stop_events.get(task_id)
            if event is not None:
                event.set()
        results = await asyncio.gather(*(self.stop(task_id) for task_id in ids), return_exceptions=True)
        for task_id, result in zip(ids, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                self.log.warning("Stopping task %s raised %s: %s", task_id, type(result).__name__, result)
        return sum(result is True for result in results)

    def snapshots(self) -> list[TaskSnapshot]:
        raw = read_json(self.config.tasks_file, {"tasks": []})
        return [TaskSnapshot(**item) for item in raw.get("tasks", [])]

    def _save_snapshot(self, snapshot: TaskSnapshot) -> None:
        snapshot.updated_at = utc_now_iso()
        record = snapshot.to_dict()

        def _mutate(raw: Any) -> dict[str, Any]:
            tasks = list(raw.get("tasks", [])) if isinstance(raw, dict) else []
            for index, item in enumerate(tasks):
                if isinstance(item, dict) and item.get("id") == snapshot.id:
                    tasks[index] = record
                    break
            else:
                tasks.append(record)
            return {"tasks": tasks[-200:]}

        update_json(self.config.tasks_file, {"tasks": []}, _mutate)
