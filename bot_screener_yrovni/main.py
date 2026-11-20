"""Volume Profile скринер — POC + дневные уровни.

Запуск:  python bot_screener_yrovni/main.py
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
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "max_price": get_env_float("MAX_PRICE", 0.0),
        "num_bins": 100,
        "proximity_pct": get_env_float("PROXIMITY_PCT", 10.0),
        "exclude_symbols": exclude,
    }


async def scan_once(cfg: dict, metrics: ScreenerMetrics) -> None:
    """Один прогон сканирования."""
    from bot_screener_klin.fetcher import Fetcher
    from bot_screener_yrovni.history import load_all_time
    from bot_screener_yrovni.printer import print_results
    from bot_screener_yrovni.scanner import (
        interval_limits,
        scan_symbol,
        windows_from_series,
    )

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
        metrics=metrics,
    )

    start = time.monotonic()

    logger.info("Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())

    exclude = set(cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info(
        "Найдено %d символов с оборотом > $%sM (всего: %d, исключено: %d)",
        len(filtered),
        cfg["min_turnover_24h"] / 1_000_000,
        total_symbols,
        len(exclude),
    )

    results = []
    limits = interval_limits()

    for symbol, _ in filtered:
        # Одна серия на интервал: окна нарезаются из неё, без лишних запросов
        by_interval: dict[str, list[dict]] = {}

        for interval, limit in limits.items():
            candles = await fetcher.get_klines(
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            if candles:
                by_interval[interval] = candles

        if not by_interval:
            continue

        all_time = await load_all_time(fetcher, symbol)

        result = scan_symbol(
            symbol=symbol,
            series=windows_from_series(by_interval),
            all_time=all_time,
            num_bins=cfg["num_bins"],
            proximity_pct=cfg["proximity_pct"],
            max_price=cfg["max_price"],
        )

        if result.levels:
            results.append(result)
            logger.info(
                "СИГНАЛ: %s price=%.6g уровней=%d",
                result.symbol,
                result.current_price,
                len(result.levels),
            )

    elapsed = time.monotonic() - start

    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
    )


async def main() -> None:
    """Главный цикл скринера."""
    setup_logging("logs/screener_vp.log", "INFO")
    cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "VP-скринер запущен: interval=%ss, min_turnover=$%sM, proximity=%.1f%%",
        cfg["scan_interval"],
        cfg["min_turnover_24h"] / 1_000_000,
        cfg["proximity_pct"],
    )

    while True:
        try:
            await scan_once(cfg, metrics)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        if metrics.scan_count % 5 == 0 and metrics.scan_count > 0:
            logger.info("МЕТРИКИ:\n%s", metrics.report())

        logger.info("Следующее сканирование через %d сек...", cfg["scan_interval"])
        await asyncio.sleep(cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
