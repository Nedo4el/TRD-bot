"""  — новый скринер.

Запуск:  python bot_screener_uzkiy/main.py
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

from bot_screener_uzkiy.printer import print_results
from bot_screener_uzkiy.scanner import ScanConfig, ScanResult, analyze_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)


def _get_turnover(tickers_map: dict[str, dict], symbol: str) -> float:
    """Получить оборот за 24ч из тикера."""
    t = tickers_map.get(symbol, {})
    vol = t.get("turnover24h", "0")
    try:
        return float(vol)
    except (ValueError, TypeError):
        return 0.0


def _load_config() -> tuple[dict, ScanConfig]:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "D"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 3600),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 1_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100),
        "exclude_symbols": exclude,
    }

    scan_cfg = ScanConfig()

    return env_cfg, scan_cfg


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    lookback: int,
    cfg: ScanConfig,
    turnover_24h: float = 0.0,
) -> ScanResult | None:
    """Просканировать один символ."""
    from bot_screener_klin.fetcher import Fetcher

    assert isinstance(fetcher, Fetcher)

    candles = await fetcher.get_klines(
        symbol=symbol,
        interval=timeframe,
        limit=lookback,
    )

    if not candles:
        return None

    result = analyze_symbol(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        cfg=cfg,
        turnover_24h=turnover_24h,
    )

    return result


async def scan_once(
    env_cfg: dict,
    scan_cfg: ScanConfig,
    metrics: ScreenerMetrics,
) -> None:
    """Один прогон сканирования."""
    from bot_screener_klin.fetcher import Fetcher

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

    results: list[ScanResult] = []

    # Получаем тикеры для оборота
    tickers_map: dict[str, dict] = {}
    tickers = await fetcher.get_linear_tickers()
    for t in tickers:
        tickers_map[t.get("symbol", "")] = t

    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(
                fetcher,
                sym,
                env_cfg["timeframe"],
                env_cfg["lookback_bars"],
                scan_cfg,
                turnover_24h=_get_turnover(tickers_map, sym),
            )
            for sym, _ in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result and result.price > 0:
                results.append(result)

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
    setup_logging("logs/screener_uzkiy.log", "INFO")
    env_cfg, scan_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер запущен: timeframe=%s, interval=%ss, "
        "min_turnover=$%sM",
        env_cfg["timeframe"],
        env_cfg["scan_interval"],
        env_cfg["min_turnover_24h"] / 1_000_000,
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
