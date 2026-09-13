"""Screener — тренд по HH/HL (восходящий) и LH/LL (нисходящий).

Запуск:  python bot_screener_trend/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_trend.fetcher import Fetcher
from bot_screener_trend.printer import print_results, save_report
from bot_screener_trend.scanner import ScanConfig, ScanResult, analyze_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))
REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")


def _load_config() -> tuple[dict, ScanConfig]:
    load_bot_env(_BOT_DIR)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 10_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100),
        "exclude_symbols": exclude,
    }

    scan_cfg = ScanConfig(
        swing_window=get_env_int("SWING_WINDOW", 5),
        ema_fast=get_env_int("EMA_FAST", 20),
        ema_slow=get_env_int("EMA_SLOW", 50),
        adx_min=get_env_float("ADX_MIN", 20.0),
        min_swings=get_env_int("MIN_SWINGS", 3),
        min_turnover_24h=env_cfg["min_turnover_24h"],
        exclude_symbols=exclude,
    )

    return env_cfg, scan_cfg


async def _scan_one(
    fetcher: Fetcher,
    symbol: str,
    timeframe: str,
    lookback: int,
    cfg: ScanConfig,
    turnover_24h: float,
) -> ScanResult | None:
    candles = await fetcher.get_klines(symbol, timeframe, limit=lookback)
    if not candles:
        return None
    candles.sort(key=lambda c: c["open_time"])
    return analyze_symbol(symbol, candles, cfg=cfg, timeframe=timeframe, turnover_24h=turnover_24h)


async def scan_once(
    env_cfg: dict,
    scan_cfg: ScanConfig,
    metrics: ScreenerMetrics,
) -> None:
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
        "Найдено %d символов (всего: %d, исключено: %d)",
        len(filtered), total_symbols, len(exclude),
    )

    results: list[ScanResult] = []

    batch_size = 5
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i: i + batch_size]
        tasks = [
            _scan_one(fetcher, sym, env_cfg["timeframe"], env_cfg["lookback_bars"], scan_cfg, turnover)
            for sym, turnover in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result:
                results.append(result)

        done = min(i + batch_size, len(filtered))
        logger.info("Прогресс: %d/%d", done, len(filtered))

    elapsed = time.monotonic() - start
    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
    )

    if REPORTS_DIR.exists():
        path = save_report(results, total_symbols, len(filtered), elapsed, str(REPORTS_DIR))
        logger.info("Отчёт: %s", path)


async def main() -> None:
    setup_logging("logs/screener_trend.log", "INFO")
    env_cfg, scan_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер тренда запущен: timeframe=%s, interval=%ss, "
        "min_turnover=$%sM, lookback=%d",
        env_cfg["timeframe"],
        env_cfg["scan_interval"],
        env_cfg["min_turnover_24h"] / 1_000_000,
        env_cfg["lookback_bars"],
    )

    while True:
        try:
            await scan_once(env_cfg, scan_cfg, metrics)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        if metrics.scan_count % 5 == 0 and metrics.scan_count > 0:
            logger.info("МЕТРИКИ:\n%s", metrics.report())

        logger.info("Следующее сканирование через %d сек...", env_cfg["scan_interval"])
        await asyncio.sleep(env_cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
