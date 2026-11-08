"""Планировщик фонового сканирования."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.logger import get_logger
from dashboard.adapters import ADAPTERS
from dashboard.config import DashboardConfig, ScreenerSpec
from dashboard.store import SignalStore

logger = get_logger(__name__)


@dataclass(slots=True)
class _ScreenerClock:
    """Абсолютное расписание: следующий слот не сдвигается после задержки."""

    interval: int
    anchor: float

    def tick(self) -> float:
        """Вернуть длительность паузы до следующего запуска."""
        now = time.monotonic()
        due = self.anchor + self.interval
        while due <= now:
            due += self.interval
        self.anchor = due
        return max(0.0, due - now)


class Scheduler:
    """Крутит все скринеры по своим интервалам."""

    def __init__(
        self,
        specs: list[ScreenerSpec],
        store: SignalStore,
        config: DashboardConfig,
    ) -> None:
        self._specs = specs
        self._store = store
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._events = {spec.name: asyncio.Event() for spec in specs}
        self._clocks: dict[str, _ScreenerClock] = {
            spec.name: _ScreenerClock(spec.interval_sec, time.monotonic())
            for spec in specs
        }
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    def start(self) -> None:
        """Запустить фоновые задачи скринеров (со стартовым джиттера)."""
        for spec in self._specs:
            delay = random.uniform(0.0, 2.0)
            self._tasks.append(asyncio.create_task(self._loop(spec, delay)))

    async def stop(self) -> None:
        """Остановить фоновые задачи."""
        self._stopping = True
        for event in self._events.values():
            event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def trigger(self, name: str | None = None) -> bool:
        """Запустить скринер (или все) вне расписания."""
        if name is None:
            for event in self._events.values():
                event.set()
            return True
        target = self._events.get(name)
        if target is None:
            return False
        target.set()
        return True

    async def _loop(self, spec: ScreenerSpec, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        while not self._stopping:
            await self._run(spec)
            await self._wait(spec)

    async def _run(self, spec: ScreenerSpec) -> None:
        clock = self._clocks[spec.name]
        async with self._semaphore:
            self._store.mark_running(spec.name)
            started = time.monotonic()
            scan = ADAPTERS[spec.name]
            try:
                signals = await scan(spec)
            except Exception as exc:
                duration = time.monotonic() - started
                next_run = self._next_run(spec.interval_sec, clock)
                logger.exception("Скринер %s завершился ошибкой", spec.name)
                self._store.set_error(
                    spec.name,
                    f"{type(exc).__name__}: {exc}",
                    duration,
                    next_run,
                )
            else:
                duration = time.monotonic() - started
                next_run = self._next_run(spec.interval_sec, clock)
                logger.info(
                    "Скринер %s: %d сигналов за %.1fс",
                    spec.name,
                    len(signals),
                    duration,
                )
                self._store.set_result(
                    spec.name,
                    signals,
                    duration,
                    next_run,
                )

    async def _wait(self, spec: ScreenerSpec) -> None:
        event = self._events[spec.name]
        event.clear()
        clock = self._clocks[spec.name]
        try:
            await asyncio.wait_for(event.wait(), timeout=clock.tick())
        except TimeoutError:
            pass

    @staticmethod
    def _next_run(interval_sec: int, clock: _ScreenerClock | None = None) -> str:
        moment = datetime.now(timezone.utc) + timedelta(
            seconds=clock.tick() if clock else interval_sec
        )
        return moment.isoformat(timespec="seconds")
