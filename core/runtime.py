from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .manager import MonitoringManager
from .repository import MonitorStorage

Dispatch = Callable[[], Awaitable[dict[str, int]]]


class PluginRuntime:
    def __init__(
        self,
        manager: MonitoringManager,
        dispatch: Dispatch,
        *,
        interval_seconds: int,
        max_workers: int,
        daily_summary_time: str,
        backup_dir: Path,
    ) -> None:
        parsed = datetime.strptime(daily_summary_time, "%H:%M")
        self.manager = manager
        self.storage: MonitorStorage = manager.storage
        self.dispatch = dispatch
        self.interval_seconds = interval_seconds
        self.max_workers = max_workers
        self.daily_summary_time = time(parsed.hour, parsed.minute)
        self.backup_dir = backup_dir
        self.local_timezone = ZoneInfo("Asia/Shanghai")
        self.tasks: list[asyncio.Task] = []
        self.started_at: str | None = None
        self.last_cycle_at: str | None = None
        self.last_cycle_ok: bool | None = None
        self.last_error: str | None = None
        self.next_daily_summary_at: str | None = None
        self.last_daily_summary_at: str | None = None
        self.last_daily_summary_ok: bool | None = None
        self.last_daily_summary_error: str | None = None
        self._last_backup_date = None

    def start(self) -> None:
        if any(not task.done() for task in self.tasks):
            return
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.tasks = [
            asyncio.create_task(self._monitor_loop(), name="steam-skin-ops-monitor"),
            asyncio.create_task(self._summary_loop(), name="steam-skin-ops-summary"),
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.tasks.clear()
        self.next_daily_summary_at = None

    async def _monitor_loop(self) -> None:
        while True:
            try:
                results = await asyncio.to_thread(
                    self.manager.run_cycle, self.max_workers
                )
                await self.dispatch()
                self.last_cycle_ok = all(row.get("success", True) for row in results)
                self.last_error = None
                now = datetime.now(timezone.utc)
                today = now.astimezone(self.local_timezone).date()
                if self._last_backup_date != today:
                    await asyncio.to_thread(self.storage.prune_snapshots, 30)
                    await asyncio.to_thread(self._backup)
                    self._last_backup_date = today
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_cycle_ok = False
                self.last_error = str(exc)
            finally:
                self.last_cycle_at = datetime.now(timezone.utc).isoformat()
            await asyncio.sleep(self.interval_seconds)

    def _next_summary(self, now: datetime) -> datetime:
        local = now.astimezone(self.local_timezone)
        target = datetime.combine(local.date(), self.daily_summary_time, self.local_timezone)
        completed = self.storage.get_metadata("daily_summary:last_date")
        if local < target:
            return target
        if completed != local.date().isoformat():
            return local
        return target + timedelta(days=1)

    async def _summary_loop(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            target = await asyncio.to_thread(self._next_summary, now)
            self.next_daily_summary_at = target.isoformat()
            delay = max(0.0, (target - now.astimezone(self.local_timezone)).total_seconds())
            if delay:
                await asyncio.sleep(delay)
                continue
            self.last_daily_summary_at = now.isoformat()
            try:
                await asyncio.to_thread(
                    self.manager.enqueue_daily_summary,
                    now.astimezone(self.local_timezone).date(),
                )
                await self.dispatch()
                self.last_daily_summary_ok = True
                self.last_daily_summary_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_daily_summary_ok = False
                self.last_daily_summary_error = str(exc)
                await asyncio.sleep(300)

    def _backup(self, retain: int = 7) -> Path:
        destination = self.backup_dir / f"monitor-{datetime.now().strftime('%Y%m%d')}.db"
        self.storage.backup(destination)
        backups = sorted(self.backup_dir.glob("monitor-*.db"), reverse=True)
        for old in backups[retain:]:
            old.unlink(missing_ok=True)
        return destination

    def status(self) -> dict:
        running = len(self.tasks) == 2 and all(not task.done() for task in self.tasks)
        return {
            "running": running,
            "started_at": self.started_at,
            "last_cycle_at": self.last_cycle_at,
            "last_cycle_ok": self.last_cycle_ok,
            "last_error": self.last_error,
            "next_daily_summary_at": self.next_daily_summary_at,
            "last_daily_summary_at": self.last_daily_summary_at,
            "last_daily_summary_ok": self.last_daily_summary_ok,
            "last_daily_summary_error": self.last_daily_summary_error,
            "items": self.storage.count_rule_items(),
            "rules": len(self.storage.list_rules()),
            "outbox": self.storage.outbox_counts(),
        }
