"""Точка входа robot_zakol: SIGINT/SIGTERM, recovery, event loop."""

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
from robot_zakol.config import ZakolConfig, load_config
from robot_zakol.data_feed import DataFeed
from robot_zakol.order_cycle import OrderCycle
from robot_zakol.state import StateStore

logger = logging.getLogger(__name__)

# сколько секунд ждать корректной остановки цикла после сигнала
SHUTDOWN_TIMEOUT = 10.0
# сколько секунд ждать поднятия WS перед стартом торговли (A.5)
WS_CONNECT_TIMEOUT = 10.0


async def _wait_ws_connected(feed: Any, timeout: float) -> bool:
    """Не выходим из стартового блока, пока WS не поднялся."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if feed.is_connected():
            return True
        await asyncio.sleep(0.2)
    return bool(feed.is_connected())


def _core_config(zakol: ZakolConfig) -> Config:
    return Config(
        api_key=zakol.api_key,
        api_secret=zakol.api_secret,
        testnet=zakol.testnet,
        simulation_mode=False,
        category=zakol.category,
        symbol=zakol.symbol,
        requests_per_second=zakol.requests_per_second,
        ws_enabled=False,
        log_level=zakol.log_level,
        log_file=zakol.log_file,
        state_file=zakol.state_file,
        stop_loss_pct=zakol.stop_pct * 100,
        take_profit_pct=0.0,
        position_pct=zakol.position_pct,
        fixed_qty=zakol.fixed_qty,
        recv_window=zakol.recv_window,
        time_sync=zakol.time_sync,
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
    zakol = load_config()
    setup_logging(zakol.log_file, zakol.log_level)
    zakol.validate_for_live()
    cfg = _core_config(zakol)
    client = BybitClient(cfg)
    loop = asyncio.get_running_loop()
    notifier = Notifier(cfg)
    feed = DataFeed(
        cfg,
        loop,
        cfg.symbol,
        notifier.notify,
        reconnect_sec=zakol.reconnect_sec,
        heartbeat_sec=zakol.heartbeat_sec,
    )
    store = StateStore(cfg.state_path, symbol=zakol.symbol)
    stop_event = asyncio.Event()
    cycle: OrderCycle | None = None
    runner: asyncio.Task[Any] | None = None
    stopper: asyncio.Task[Any] | None = None

    try:
        if zakol.time_sync:
            # TIME_SYNC: сверка часов с биржей, лечение retCode 10002
            await client.sync_time()
        filters = await client.get_instrument_filters(cfg.symbol)
        # A.2: hedge-режим боту не подходит (positionIdx=0)
        mode = await client.get_position_mode(cfg.symbol)
        if mode == "hedge":
            raise ValueError(
                "аккаунт в HEDGE-режиме — переключи Position Mode на Single",
            )
        if mode == "unknown":
            logger.warning("режим позиции не проверен — ожидается one-way")
        cycle = OrderCycle(
            cfg=zakol,
            client=client,
            feed=feed,
            store=store,
            tick_size=filters.tick_size,
            qty_step=filters.qty_step,
            notifier=notifier,
        )

        def _signal_handler() -> None:
            logger.info("signal: graceful shutdown")
            stop_event.set()
            cycle.request_stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda *_: _signal_handler())

        await cycle.recover()
        if zakol.ws_enabled:
            feed.start()
            if not await _wait_ws_connected(feed, WS_CONNECT_TIMEOUT):
                raise RuntimeError(
                    f"WS не подключился за {WS_CONNECT_TIMEOUT:.0f}с — старт отменён",
                )
        logger.info(
            "robot_zakol started symbol=%s testnet=%s",
            cfg.symbol,
            cfg.testnet,
        )
        runner = asyncio.create_task(cycle.run())
        stopper = asyncio.create_task(stop_event.wait())
        await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        # сначала цикл должен корректно завершиться (finally → _on_shutdown),
        # и только потом закрываем фид/клиент/стейт
        cycle.request_stop()
        if stopper is not None:
            stopper.cancel()
        await _drain(runner)
    finally:
        if cycle is not None:
            cycle.request_stop()
        if stopper is not None and not stopper.done():
            stopper.cancel()
        if runner is not None and not runner.done():
            await _drain(runner)
        feed.stop()
        client.close()
        await store.save()
        logger.info("robot_zakol stopped")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.info("keyboard interrupt")


if __name__ == "__main__":
    main()
