from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from core.config import AppConfig
from core.models import TaskSnapshot, utc_now_iso
from core.storage import read_json, write_json_atomic


class TaskRunner:
    def __init__(self, config: AppConfig):
        self.config = config
        self.tasks: dict[str, asyncio.Task] = {}
        self.stop_events: dict[str, asyncio.Event] = {}
        self.log = logging.getLogger("tasks")
        # Persisted 'running' records have no live task after a process restart.
        for snapshot in self.snapshots():
            if snapshot.status == "running":
                snapshot.status = "interrupted"
                snapshot.detail = "Previous process ended before completion"
                self._save_snapshot(snapshot)

    def start(self, name: str, coro_factory: Callable[[asyncio.Event], Awaitable[Any]]) -> str:
        active = next((task for task in self.tasks.values() if not task.done()), None)
        if active is not None:
            raise RuntimeError(f"Task '{active.get_name()}' is already running. Stop it before starting '{name}'.")
        task_id = uuid.uuid4().hex[:12]
        stop_event = asyncio.Event()
        self.stop_events[task_id] = stop_event
        self._save_snapshot(TaskSnapshot(id=task_id, name=name, status="running"))

        async def wrapper() -> Any:
            try:
                result = await coro_factory(stop_event)
            except asyncio.CancelledError:
                self._save_snapshot(
                    TaskSnapshot(id=task_id, name=name, status="stopped", detail="Cancelled")
                )
                raise
            except Exception as exc:
                self.log.exception("Task %s failed", task_id)
                self._save_snapshot(
                    TaskSnapshot(id=task_id, name=name, status="failed", detail=str(exc))
                )
                raise
            else:
                self._save_snapshot(TaskSnapshot(id=task_id, name=name, status="done"))
                return result

        self.tasks[task_id] = asyncio.create_task(wrapper(), name=name)
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
                self._save_snapshot(TaskSnapshot(id=task_id, name=task.get_name(), status="stop-timeout", detail="Cleanup is still running"))
                raise RuntimeError("Task cleanup did not finish; session remains busy") from None
        # wrapper owns the terminal state. Do not overwrite failed/done with
        # 'stopped' merely because completion happened during a stop request.
        return True

    async def stop_by_name_prefix(self, prefix: str) -> int:
        stopped = 0
        for task_id, task in list(self.tasks.items()):
            if task.done() or not task.get_name().startswith(prefix):
                continue
            if await self.stop(task_id):
                stopped += 1
        return stopped

    async def stop_all(self) -> int:
        ids = [task_id for task_id, task in self.tasks.items() if not task.done()]
        for task_id in ids:
            self.stop_events[task_id].set()
        results = await asyncio.gather(*(self.stop(task_id) for task_id in ids), return_exceptions=True)
        return sum(result is True for result in results)

    def snapshots(self) -> list[TaskSnapshot]:
        raw = read_json(self.config.tasks_file, {"tasks": []})
        return [TaskSnapshot(**item) for item in raw.get("tasks", [])]

    def _save_snapshot(self, snapshot: TaskSnapshot) -> None:
        raw = read_json(self.config.tasks_file, {"tasks": []})
        tasks = raw.get("tasks", [])
        snapshot.updated_at = utc_now_iso()
        replaced = False
        for index, item in enumerate(tasks):
            if item["id"] == snapshot.id:
                tasks[index] = snapshot.to_dict()
                replaced = True
                break
        if not replaced:
            tasks.append(snapshot.to_dict())
        write_json_atomic(self.config.tasks_file, {"tasks": tasks[-200:]})
