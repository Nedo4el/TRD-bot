"""Точка входа robot_flat: SIGINT/SIGTERM, recovery, event loop."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

# запуск `python robot_flat/main.py` (локально и в Docker): core выше по пути
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.bybit_client import BybitClient
from core.config import Config
from core.logger import setup_logging
from core.notifier import Notifier
from robot_flat.config import FlatConfig, load_config
from robot_flat.data_feed import DataFeed
from robot_flat.order_flow import OrderFlow
from robot_flat.state import StateStore
from robot_flat.strategy import FlatParams, FlatStrategy

logger = logging.getLogger(__name__)

# сколько секунд ждать корректной остановки цикла после сигнала
SHUTDOWN_TIMEOUT = 10.0
# сколько секунд ждать поднятия WS перед стартом торговли
WS_CONNECT_TIMEOUT = 10.0


async def _wait_ws_connected(feed: Any, timeout: float) -> bool:
    """Не выходим из стартового блока, пока WS не поднялся."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if feed.is_connected():
            return True
        await asyncio.sleep(0.2)
    return bool(feed.is_connected())


def _core_config(flat: FlatConfig) -> Config:
    """Собрать общий Config для клиента/логирования из FlatConfig."""
    return Config(
        api_key=flat.api_key,
        api_secret=flat.api_secret,
        testnet=flat.testnet,
        simulation_mode=False,
        category=flat.category,
        symbol=flat.symbol,
        timeframe=flat.timeframe,
        requests_per_second=flat.requests_per_second,
        ws_enabled=False,
        log_level=flat.log_level,
        log_file=flat.log_file,
        state_file=flat.state_file,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        position_pct=flat.position_pct,
        fixed_qty=flat.fixed_qty,
        recv_window=flat.recv_window,
        time_sync=flat.time_sync,
    )


async def _drain(task: asyncio.Task[Any]) -> None:
    """Дождаться цикла после остановки, не скрывая его ошибки."""
    try:
        await asyncio.wait_for(task, SHUTDOWN_TIMEOUT)
    except TimeoutError:
        logger.warning("цикл не остановился за %.0fс — отмена", SHUTDOWN_TIMEOUT)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("цикл завершился с ошибкой: %s", exc)


async def _amain() -> None:
    flat = load_config()
    setup_logging(flat.log_file, flat.log_level)
    flat.validate_for_live()
    cfg = _core_config(flat)
    client = BybitClient(cfg)
    loop = asyncio.get_running_loop()
    notifier = Notifier(cfg)
    feed = DataFeed(
        cfg,
        loop,
        cfg.symbol,
        notifier.notify,
        reconnect_sec=flat.reconnect_sec,
        heartbeat_sec=flat.heartbeat_sec,
        timeframe=flat.timeframe,
    )
    store = StateStore(cfg.state_path, symbol=flat.symbol)
    stop_event = asyncio.Event()
    flow: OrderFlow | None = None
    runner: asyncio.Task[Any] | None = None
    stopper: asyncio.Task[Any] | None = None

    try:
        if flat.time_sync:
            # TIME_SYNC: сверка часов с биржей, лечение retCode 10002
            await client.sync_time()
        filters = await client.get_instrument_filters(cfg.symbol)
        # hedge-режим боту не подходит (positionIdx=0)
        mode = await client.get_position_mode(cfg.symbol)
        if mode == "hedge":
            raise ValueError(
                "аккаунт в HEDGE-режиме — переключи Position Mode на Single",
            )
        if mode == "unknown":
            logger.warning("режим позиции не проверен — ожидается one-way")
        flow = OrderFlow(
            cfg=flat,
            client=client,
            feed=feed,
            store=store,
            notifier=notifier,
            strategy=FlatStrategy(FlatParams.from_env()),
            filters=filters,
        )

        def _signal_handler() -> None:
            logger.info("signal: graceful shutdown")
            stop_event.set()
            flow.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_: _signal_handler())

        if not await flow.recover():
            raise RuntimeError("recover не удался — биржа недоступна")
        if flat.ws_enabled:
            feed.start()
            if not await _wait_ws_connected(feed, WS_CONNECT_TIMEOUT):
                raise RuntimeError(
                    f"WS не подключился за {WS_CONNECT_TIMEOUT:.0f}с — старт отменён",
                )
        logger.info(
            "robot_flat started symbol=%s tf=%s testnet=%s",
            cfg.symbol,
            flat.timeframe,
            cfg.testnet,
        )
        runner = asyncio.create_task(flow.run())
        stopper = asyncio.create_task(stop_event.wait())
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        # сначала цикл должен корректно завершиться (finally → _on_shutdown),
        # и только потом закрываем фид/клиент/стейт
        flow.request_stop()
        if stopper is not None:
            stopper.cancel()
        await _drain(runner)
    finally:
        if flow is not None:
            flow.request_stop()
        if stopper is not None and not stopper.done():
            stopper.cancel()
        if runner is not None and not runner.done():
            await _drain(runner)
        feed.stop()
        client.close()
        await store.save()
        logger.info("robot_flat stopped")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("keyboard interrupt")


if __name__ == "__main__":
    main()
