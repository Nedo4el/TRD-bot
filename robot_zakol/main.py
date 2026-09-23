"""Точка входа robot_zakol: SIGINT/SIGTERM, recovery, event loop."""

from __future__ import annotations

import asyncio
import logging
import signal

from core.bybit_client import BybitClient
from core.config import Config
from core.logger import setup_logging
from robot_zakol.config import ZakolConfig, load_config
from robot_zakol.data_feed import DataFeed
from robot_zakol.order_cycle import OrderCycle
from robot_zakol.state import StateStore

logger = logging.getLogger(__name__)


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
    )


async def _amain() -> None:
    zakol = load_config()
    setup_logging(zakol.log_file, zakol.log_level)
    zakol.validate_for_live()
    cfg = _core_config(zakol)
    client = BybitClient(cfg)
    filters = await client.get_instrument_filters(cfg.symbol)
    loop = asyncio.get_running_loop()
    feed = DataFeed(cfg, loop, cfg.symbol)
    store = StateStore(cfg.state_path)
    cycle = OrderCycle(
        cfg=zakol,
        client=client,
        feed=feed,
        store=store,
        tick_size=filters.tick_size,
    )
    stop_event = asyncio.Event()

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
    logger.info("robot_zakol started symbol=%s testnet=%s", cfg.symbol, cfg.testnet)
    runner = asyncio.create_task(cycle.run())
    stopper = asyncio.create_task(stop_event.wait())
    await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
    cycle.request_stop()
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
