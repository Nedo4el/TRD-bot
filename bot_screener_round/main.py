"""Скринер круглых чисел — поиск близости к психологическим уровням.

Запуск:  python bot_screener_round/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_round.printer import print_results
from bot_screener_round.scanner import RoundSignal, scan_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)


def _load_config() -> dict:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "5"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 10_000_000),
        "proximity_pct": get_env_float("PROXIMITY_PCT", 5.0),
        "max_price": get_env_float("MAX_PRICE", 0),
        "exclude_symbols": exclude,
    }


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    proximity_pct: float,
    max_price: float,
    turnover: float,
) -> RoundSignal | None:
    """Просканировать один символ."""
    from bot_screener_uzkiy.fetcher import Fetcher

    assert isinstance(fetcher, Fetcher)

    candles = await fetcher.get_klines(
        symbol=symbol,
        interval=timeframe,
        limit=1,
    )

    if not candles:
        return None

    price = candles[-1]["close"]
    if price <= 0:
        return None

    return scan_symbol(
        symbol=symbol,
        timeframe=f"{timeframe}m",
        price=price,
        proximity_pct=proximity_pct,
        max_price=max_price,
        turnover_24h=turnover,
    )


async def scan_once(
    env_cfg: dict,
    metrics: ScreenerMetrics,
) -> None:
    """Один прогон сканирования."""
    from bot_screener_uzkiy.fetcher import Fetcher

    fetcher = Fetcher(
        api_key=env_cfg["api_key"],
        api_secret=env_cfg["api_secret"],
        testnet=env_cfg["testnet"],
        metrics=metrics,
    )

    start = time.monotonic()

    logger.info("Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(env_cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())

    exclude = set(env_cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info(
        "Найдено %d символов с оборотом > $%sM (всего: %d, исключено: %d)",
        len(filtered),
        env_cfg["min_turnover_24h"] / 1_000_000,
        total_symbols,
        len(exclude),
    )

    results: list[RoundSignal] = []

    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(
                fetcher,
                sym,
                env_cfg["timeframe"],
                env_cfg["proximity_pct"],
                env_cfg["max_price"],
                turnover,
            )
            for sym, turnover in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result is not None:
                results.append(result)

    results.sort(key=lambda r: r.proximity_pct)
    elapsed = time.monotonic() - start

    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
        proximity_pct=env_cfg["proximity_pct"],
    )


async def main() -> None:
    """Главный цикл скринера."""
    setup_logging("logs/screener_round.log", "INFO")
    env_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер круглых чисел запущен: timeframe=%s, interval=%ss, "
        "proximity=%.1f%%, min_turnover=$%sM",
        env_cfg["timeframe"],
        env_cfg["scan_interval"],
        env_cfg["proximity_pct"],
        env_cfg["min_turnover_24h"] / 1_000_000,
    )

    while True:
        try:
            await scan_once(env_cfg, metrics)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        if metrics.scan_count % 10 == 0 and metrics.scan_count > 0:
            logger.info("МЕТРИКИ:\n%s", metrics.report())

        logger.info("Следующее сканирование через %d сек...", env_cfg["scan_interval"])
        await asyncio.sleep(env_cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
