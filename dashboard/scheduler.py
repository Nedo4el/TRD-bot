"""Планировщик скринеров: запуск только вручную.

Фоновых интервалов нет: каждый скринер ждёт события «Сканировать»
(кнопка в UI, POST /api/scan/{name} или сохранение настроек).
Один клик = один скан.
"""

from __future__ import annotations

import asyncio
import time

from core.logger import get_logger
from dashboard.adapters import ADAPTERS
from dashboard.config import DashboardConfig, ScreenerSpec
from dashboard.store import SignalStore

logger = get_logger(__name__)


class Scheduler:
    """Запускает скринеры только по ручному событию."""

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
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    def start(self) -> None:
        """Создать фоновые задачи (они ждут ручного события)."""
        for spec in self._specs:
            self._tasks.append(asyncio.create_task(self._loop(spec)))

    async def stop(self) -> None:
        """Остановить фоновые задачи."""
        self._stopping = True
        for event in self._events.values():
            event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def trigger(self, name: str | None = None) -> bool:
        """Запустить скринер (или все) вручную."""
        if name is None:
            for event in self._events.values():
                event.set()
            return True
        target = self._events.get(name)
        if target is None:
            return False
        target.set()
        return True

    async def _loop(self, spec: ScreenerSpec) -> None:
        event = self._events[spec.name]
        while not self._stopping:
            await event.wait()
            event.clear()
            await self._run(spec)

    async def _run(self, spec: ScreenerSpec) -> None:
        async with self._semaphore:
            self._store.mark_running(spec.name)
            started = time.monotonic()
            scan = ADAPTERS[spec.name]
            try:
                signals = await scan(spec)
            except Exception as exc:
                duration = time.monotonic() - started
                logger.exception("Скринер %s завершился ошибкой", spec.name)
                self._store.set_error(
                    spec.name,
                    f"{type(exc).__name__}: {exc}",
                    duration,
                    "",
                )
            else:
                duration = time.monotonic() - started
                logger.info(
                    "Скринер %s: %d сигналов за %.1fс",
                    spec.name,
                    len(signals),
                    duration,
                )
                self._store.set_result(spec.name, signals, duration, "")
