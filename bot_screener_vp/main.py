"""Volume Profile скринер — Bybit USDT-M фьючерсы.

Запуск:  python bot_screener_vp/main.py
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

logger = get_logger(__name__)


def _load_config() -> dict:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

    raw_timeframes = os.getenv("TIMEFRAMES", "5")
    timeframes = [t.strip() for t in raw_timeframes.split(",") if t.strip()]

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframes": timeframes,
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 500),
        "num_levels": get_env_int("NUM_LEVELS", 10),
        "num_bins": get_env_int("NUM_BINS", 100),
        "proximity_pct": get_env_float("PROXIMITY_PCT", 5.0),
        "exclude_symbols": exclude,
    }


async def scan_once(cfg: dict) -> None:
    """Один прогон сканирования."""
    from bot_screener.fetcher import Fetcher
    from bot_screener_vp.printer import print_results
    from bot_screener_vp.scanner import scan_symbol

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
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

    for symbol, turnover in filtered:
        for timeframe in cfg["timeframes"]:
            candles = await fetcher.get_klines(
                symbol=symbol,
                interval=timeframe,
                limit=cfg["lookback_bars"],
            )

            if not candles:
                continue

            result = scan_symbol(
                symbol=symbol,
                timeframe=f"{timeframe}m",
                candles=candles,
                num_levels=cfg["num_levels"],
                num_bins=cfg["num_bins"],
                proximity_pct=cfg["proximity_pct"],
            )

            if result.levels:
                results.append(result)
                logger.info(
                    "СИГНАЛ: %s [%s] levels=%d price=%.4f",
                    result.symbol,
                    result.timeframe,
                    len(result.levels),
                    result.current_price,
                )

    elapsed = time.monotonic() - start

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

    logger.info(
        "VP-скринер запущен: timeframes=%s, interval=%ss, proximity=%.1f%%, levels=%d",
        ",".join(cfg["timeframes"]),
        cfg["scan_interval"],
        cfg["proximity_pct"],
        cfg["num_levels"],
    )

    while True:
        try:
            await scan_once(cfg)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        logger.info("Следующее сканирование через %d сек...", cfg["scan_interval"])
        await asyncio.sleep(cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
