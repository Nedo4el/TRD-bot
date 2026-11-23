"""Планировщик скринеров: ручной запуск и автоскан POC по таймеру.

Ручной режим: каждый скринер ждёт события «Сканировать» (кнопка в UI,
POST /api/scan/{name}). Авторежим: фоновая петля раз в ``interval_sec``
поднимает POC-скринер; режим и интервал читаются из JSON на каждом тике,
поэтому переключаются без перезапуска сервера.

Сигналы POC, найденные сканом дашборда, отправляются в тот же приёмник, что
и POST от CLI-скринера: ``pending`` при AUTO_TRADE=false, ``active`` при
true. Робот уровней забирает только ``active``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from core.logger import get_logger
from dashboard import progress
from dashboard.adapters import ADAPTERS
from dashboard.config import DashboardConfig, ScreenerSpec
from dashboard.models import Signal
from dashboard.poc_signals import PocSignalStore
from dashboard.scan_mode import AUTO_SCREENER, ScanModeSettings
from dashboard.store import SignalStore

logger = get_logger(__name__)

# Как часто петля автоскана перечитывает режим и проверяет таймер.
AUTO_POLL_SEC = 5.0


def poc_payload(signal: Signal) -> dict[str, Any] | None:
    """Превратить сигнал дашборда в тело ``POST /api/signal`` (POC_CLUSTER).

    Возвращает ``None``, если у сигнала нет POC-идентификатора: такие записи
    (например, карточка «Все монеты») роботу не нужны.
    """
    detail = signal.detail or {}
    signal_id = str(detail.get("signal_id") or "").strip()
    direction = str(signal.side or "").strip().lower()
    if not signal_id or direction not in ("long", "short") or signal.price <= 0:
        return None
    windows = str(detail.get("windows") or "")
    return {
        "type": "POC_CLUSTER",
        "signal_id": signal_id,
        "symbol": signal.symbol,
        "direction": direction,
        "entry_price": signal.price,
        "price": float(detail.get("price") or 0.0),
        "distance_pct": float(detail.get("distance_pct") or 0.0),
        "zone_pct": float(detail.get("zone_pct") or 0.0),
        "volume": float(detail.get("volume") or 0.0),
        "members": [],
        "windows": [name for name in windows.split(",") if name],
        "ts": signal.found_at,
    }


class Scheduler:
    """Запускает скринеры по ручному событию и автоскан POC по таймеру."""

    def __init__(
        self,
        specs: list[ScreenerSpec],
        store: SignalStore,
        config: DashboardConfig,
        poc_store: PocSignalStore | None = None,
        *,
        poll_sec: float = AUTO_POLL_SEC,
        mode_loader: Callable[[], ScanModeSettings] = ScanModeSettings.load,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._specs = specs
        self._store = store
        self._poc_store = poc_store
        self._poll_sec = max(poll_sec, 0.0)
        self._mode_loader = mode_loader
        self._clock = clock
        self._semaphore = asyncio.Semaphore(config.concurrency)
        self._events = {spec.name: asyncio.Event() for spec in specs}
        self._tasks: list[asyncio.Task[None]] = []
        self._auto_task: asyncio.Task[None] | None = None
        self._last_auto = 0.0
        self._stopping = False

    def start(self) -> None:
        """Создать фоновые задачи: ожидание ручного события и автоскан."""
        for spec in self._specs:
            self._tasks.append(asyncio.create_task(self._loop(spec)))
        self._auto_task = asyncio.create_task(self._auto_loop())

    async def stop(self) -> None:
        """Остановить фоновые задачи."""
        self._stopping = True
        for event in self._events.values():
            event.set()
        tasks = [*self._tasks]
        if self._auto_task is not None:
            tasks.append(self._auto_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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

    async def _auto_loop(self) -> None:
        """Раз в интервал поднимать POC-скринер, если включён авторежим."""
        while not self._stopping:
            settings = self._mode_loader()
            if settings.mode == "auto":
                now = self._clock()
                if now - self._last_auto >= settings.interval_sec:
                    self._last_auto = now
                    logger.info(
                        "Автоскан %s: интервал %d с",
                        AUTO_SCREENER,
                        settings.interval_sec,
                    )
                    await self.trigger(AUTO_SCREENER)
            await asyncio.sleep(self._poll_sec)

    async def _loop(self, spec: ScreenerSpec) -> None:
        event = self._events[spec.name]
        while not self._stopping:
            await event.wait()
            event.clear()
            await self._run(spec)

    async def _run(self, spec: ScreenerSpec) -> None:
        async with self._semaphore:
            self._store.mark_running(spec.name)
            progress.start(spec.name)
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
                if spec.live_signals:
                    self._feed_poc(signals)
            finally:
                progress.clear(spec.name)

    def _feed_poc(self, signals: list[Signal]) -> None:
        """Отдать найденные POC-сигналы приёмнику робота (pending/active)."""
        if self._poc_store is None:
            return
        created = 0
        try:
            for signal in signals:
                payload = poc_payload(signal)
                if payload is None:
                    continue
                _, is_new = self._poc_store.add(payload)
                created += int(is_new)
        except Exception:
            logger.exception("Не удалось передать POC-сигналы роботу")
            return
        if created:
            logger.info("POC: передано роботу новых сигналов: %d", created)
