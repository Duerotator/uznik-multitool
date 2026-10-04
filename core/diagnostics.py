from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("diagnostics")

_RESOURCE_SNAPSHOTS: deque[dict[str, Any]] = deque(maxlen=720)
_monitor_task: asyncio.Task | None = None
_monitor_stop: asyncio.Event | None = None
_installed = False


def install_global_exception_handler() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    def _asyncio_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        message = context.get("message", "Unknown async error")
        exception = context.get("exception")
        task = context.get("task")
        if exception is not None:
            tb = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
            logger.critical(
                "ASYNC LOOP ERROR | task=%s | %s\n%s",
                getattr(task, "get_name", lambda: "?")(),
                message,
                tb,
            )
        else:
            logger.critical("ASYNC LOOP ERROR | %s", message)

    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_asyncio_handler)
    except RuntimeError:
        pass

    original_excepthook = sys.excepthook

    def _sys_excepthook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            original_excepthook(exc_type, exc_value, exc_tb)
            return
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.critical("UNCAUGHT EXCEPTION (sys.excepthook):\n%s", tb_text)
        _emergency_dump("sys_excepthook", exc_type, exc_value, exc_tb)
        original_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        original_thread_hook = threading.excepthook

        def _thread_excepthook(args):  # type: ignore[no-untyped-def]
            tb_text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            logger.critical(
                "UNCAUGHT THREAD EXCEPTION in %s:\n%s",
                getattr(args, "thread", "?"),
                tb_text,
            )
            _emergency_dump("thread_excepthook", args.exc_type, args.exc_value, args.exc_traceback)
            original_thread_hook(args)

        threading.excepthook = _thread_excepthook

    logger.info("Global exception handlers installed (async loop + sys + threading)")


def _emergency_dump(source: str, exc_type: type, exc_value: BaseException, exc_tb: Any) -> None:
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        mem_info = proc.memory_info()
        dump_path = Path("data") / "logs" / "crash_dump.log"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        with dump_path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"CRASH DUMP | {time.strftime('%Y-%m-%d %H:%M:%S')} | source={source}\n")
            f.write(f"PID={os.getpid()} | PPID={os.getppid()}\n")
            f.write(f"Python {sys.version}\n")
            f.write(f"Platform: {platform.platform()}\n")
            f.write(f"RSS: {mem_info.rss / 1024 / 1024:.1f} MB\n")
            f.write(f"VMS: {mem_info.vms / 1024 / 1024:.1f} MB\n")
            try:
                cpu_pct = proc.cpu_percent(interval=0.1)
                f.write(f"CPU: {cpu_pct}%\n")
            except Exception:
                pass
            f.write(f"Threads: {threading.active_count()}\n")
            for t in threading.enumerate():
                f.write(f"  thread: {t.name} daemon={t.daemon} alive={t.is_alive()}\n")
            f.write(f"\nException:\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
            f.write(f"{'=' * 60}\n")
        logger.error("Crash dump written to %s", dump_path)
    except Exception as dump_exc:
        logger.error("Could not write crash dump: %s", dump_exc)


class ResourceMonitor:
    def __init__(
        self,
        interval: float = 5.0,
        ram_warn_mb: float = 512.0,
        ram_critical_mb: float = 1024.0,
        cpu_warn_pct: float = 85.0,
    ):
        self.interval = interval
        self.ram_warn_mb = ram_warn_mb
        self.ram_critical_mb = ram_critical_mb
        self.cpu_warn_pct = cpu_warn_pct
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        import psutil

        proc = psutil.Process(os.getpid())
        stop = stop_event or asyncio.Event()
        self._stop = stop
        logger.info(
            "Resource monitor started (interval=%.1fs, ram_warn=%.0fMB, ram_critical=%.0fMB, cpu_warn=%.0f%%)",
            self.interval,
            self.ram_warn_mb,
            self.ram_critical_mb,
            self.cpu_warn_pct,
        )
        while not stop.is_set():
            try:
                mem = proc.memory_info()
                rss_mb = mem.rss / 1024 / 1024
                vms_mb = mem.vms / 1024 / 1024
                cpu_pct = proc.cpu_percent(interval=None)
                threads = threading.active_count()
                system_mem = psutil.virtual_memory()

                snapshot: dict[str, Any] = {
                    "ts": time.time(),
                    "rss_mb": round(rss_mb, 1),
                    "vms_mb": round(vms_mb, 1),
                    "cpu_pct": round(cpu_pct, 1),
                    "threads": threads,
                    "system_used_pct": round(system_mem.percent, 1),
                    "system_avail_mb": round(system_mem.available / 1024 / 1024, 0),
                }
                _RESOURCE_SNAPSHOTS.append(snapshot)

                level = logging.DEBUG
                msg = "RES | rss=%.1fMB vms=%.1fMB cpu=%.1f%% threads=%d sys_used=%.0f%% sys_avail=%.0fMB"
                args: tuple = (rss_mb, vms_mb, cpu_pct, threads, system_mem.percent, system_mem.available / 1024 / 1024)

                if rss_mb >= self.ram_critical_mb:
                    level = logging.CRITICAL
                    msg += " | RAM CRITICAL"
                elif rss_mb >= self.ram_warn_mb:
                    level = logging.WARNING
                    msg += " | RAM WARNING"

                if cpu_pct >= self.cpu_warn_pct:
                    if level < logging.WARNING:
                        level = logging.WARNING
                    msg += " | CPU WARNING"

                logger.log(level, msg, *args)

            except Exception:
                logger.debug("Resource monitor tick failed", exc_info=True)

            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._task and not self._task.done():
            return
        stop = asyncio.Event()
        self._stop = stop
        self._task = asyncio.create_task(self.run(stop), name="resource-monitor")

    def stop(self) -> None:
        if self._stop:
            self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()

    @staticmethod
    def recent_snapshots(n: int = 10) -> list[dict[str, Any]]:
        return list(_RESOURCE_SNAPSHOTS)[-n:]

    @staticmethod
    def peak_rss_mb() -> float:
        if not _RESOURCE_SNAPSHOTS:
            return 0.0
        return max(s.get("rss_mb", 0.0) for s in _RESOURCE_SNAPSHOTS)


class ProxyDiagnostics:
    def __init__(self) -> None:
        self.log = logging.getLogger("proxy-diag")
        self._connections: dict[str, dict[str, Any]] = {}

    def trace_connect(self, ip: str, port: int, protocol: str, tag: str = "") -> None:
        key = f"{ip}:{port}/{protocol}"
        self._connections[key] = {"start": time.monotonic(), "tag": tag}
        self.log.debug("[CONNECT] %s tag=%s", key, tag)

    def trace_handshake(self, ip: str, port: int, protocol: str, ok: bool, latency: float = 0.0) -> None:
        key = f"{ip}:{port}/{protocol}"
        info = self._connections.pop(key, {})
        elapsed = time.monotonic() - info.get("start", time.monotonic())
        status = "OK" if ok else "FAIL"
        self.log.info(
            "[HANDSHAKE] %s %s latency=%.3fs total=%.3fs tag=%s",
            key, status, latency, elapsed, info.get("tag", ""),
        )

    def trace_timeout(self, ip: str, port: int, protocol: str, timeout: float) -> None:
        key = f"{ip}:{port}/{protocol}"
        self._connections.pop(key, None)
        self.log.warning("[TIMEOUT] %s after %.1fs", key, timeout)

    def trace_error(self, ip: str, port: int, protocol: str, error: Exception) -> None:
        key = f"{ip}:{port}/{protocol}"
        self._connections.pop(key, None)
        self.log.warning("[ERROR] %s: %s: %s", key, type(error).__name__, error)

    def trace_replace_proxy(self, account_id: str, old_proxy: str | None, new_proxy: str | None) -> None:
        self.log.info(
            "[PROXY_SWAP] account=%s old=%s new=%s",
            account_id, old_proxy or "(none)", new_proxy or "(none)",
        )

    def trace_client_lifecycle(self, account_id: str, event: str, detail: str = "") -> None:
        self.log.info("[CLIENT] account=%s event=%s %s", account_id, event, detail)


_proxy_diag = ProxyDiagnostics()


def get_proxy_diagnostics() -> ProxyDiagnostics:
    return _proxy_diag


def get_resource_monitor() -> ResourceMonitor:
    return ResourceMonitor()
