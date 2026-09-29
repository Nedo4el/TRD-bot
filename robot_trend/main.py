"""Точка входа robot_trend: SIGINT/SIGTERM, recovery, event loop."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any

from core.bybit_client import BybitClient
from core.config import Config
from core.logger import setup_logging
from core.notifier import Notifier
from robot_trend.config import TrendConfig, load_config
from robot_trend.data_feed import DataFeed
from robot_trend.order_flow import OrderFlow
from robot_trend.state import StateStore
from robot_trend.strategy import TrendStrategy

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


def _core_config(trend: TrendConfig) -> Config:
    """Собрать общий Config для клиента/логирования из TrendConfig."""
    return Config(
        api_key=trend.api_key,
        api_secret=trend.api_secret,
        testnet=trend.testnet,
        simulation_mode=False,
        category=trend.category,
        symbol=trend.symbol,
        timeframe=trend.timeframe,
        requests_per_second=trend.requests_per_second,
        ws_enabled=False,
        log_level=trend.log_level,
        log_file=trend.log_file,
        state_file=trend.state_file,
        stop_loss_pct=0.0,
        take_profit_pct=0.0,
        position_pct=trend.position_pct,
        fixed_qty=trend.fixed_qty,
        recv_window=trend.recv_window,
        time_sync=trend.time_sync,
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
    trend = load_config()
    setup_logging(trend.log_file, trend.log_level)
    trend.validate_for_live()
    cfg = _core_config(trend)
    client = BybitClient(cfg)
    loop = asyncio.get_running_loop()
    notifier = Notifier(cfg)
    feed = DataFeed(
        cfg,
        loop,
        cfg.symbol,
        notifier.notify,
        reconnect_sec=trend.reconnect_sec,
        heartbeat_sec=trend.heartbeat_sec,
        timeframe=trend.timeframe,
    )
    store = StateStore(cfg.state_path, symbol=trend.symbol)
    stop_event = asyncio.Event()
    flow: OrderFlow | None = None
    runner: asyncio.Task[Any] | None = None
    stopper: asyncio.Task[Any] | None = None

    try:
        if trend.time_sync:
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
            cfg=trend,
            client=client,
            feed=feed,
            store=store,
            notifier=notifier,
            strategy=TrendStrategy(),
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
        if trend.ws_enabled:
            feed.start()
            if not await _wait_ws_connected(feed, WS_CONNECT_TIMEOUT):
                raise RuntimeError(
                    f"WS не подключился за {WS_CONNECT_TIMEOUT:.0f}с — старт отменён",
                )
        logger.info(
            "robot_trend started symbol=%s tf=%s testnet=%s",
            cfg.symbol,
            trend.timeframe,
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
        logger.info("robot_trend stopped")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("keyboard interrupt")


if __name__ == "__main__":
    main()
