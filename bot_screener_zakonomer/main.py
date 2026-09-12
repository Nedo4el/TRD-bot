"""Screener — поиск закономерностей во времени всплесков объема.

Запуск:  python bot_screener_zakonomer/main.py
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

from bot_screener_zakonomer.printer import print_results
from bot_screener_zakonomer.scanner import ScanConfig, ScanResult, analyze_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))


def _load_config() -> tuple[dict, ScanConfig]:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 3600),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_days": get_env_int("LOOKBACK_DAYS", 30),
        "exclude_symbols": exclude,
    }

    scan_cfg = ScanConfig(
        lookback_days=env_cfg["lookback_days"],
        interval=env_cfg["timeframe"],
        min_volume_usd=get_env_float("MIN_VOLUME_USD", 1_000_000),
        spike_std_multiplier=get_env_float("SPIKE_STD_MULTIPLIER", 3.0),
        min_turnover_24h=env_cfg["min_turnover_24h"],
        exclude_symbols=exclude,
    )

    return env_cfg, scan_cfg


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    cfg: ScanConfig,
) -> ScanResult | None:
    """Просканировать один символ за период."""
    from bot_screener_zakonomer.fetcher import Fetcher

    assert isinstance(fetcher, Fetcher)

    candles = await fetcher.get_klines_range(
        symbol=symbol,
        interval=timeframe,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=1000,
    )

    if not candles:
        return None

    # Bybit возвращает newest-first, нам нужен oldest-first для анализа
    candles.sort(key=lambda c: c["open_time"])

    return analyze_symbol(symbol, candles, cfg=cfg)


async def scan_once(
    env_cfg: dict,
    scan_cfg: ScanConfig,
    metrics: ScreenerMetrics,
) -> None:
    """Один прогон сканирования."""
    from bot_screener_zakonomer.fetcher import Fetcher

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

    # Временной диапазон
    now_ms = int(time.time() * 1000)
    lookback_ms = env_cfg["lookback_days"] * 24 * 3600 * 1000
    start_ms = now_ms - lookback_ms

    results: list[ScanResult] = []

    batch_size = 5  # меньше батч — больше данных за символ
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(
                fetcher,
                sym,
                env_cfg["timeframe"],
                start_ms,
                now_ms,
                scan_cfg,
            )
            for sym, _ in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result and result.spikes_found > 0:
                results.append(result)

        # Прогресс
        done = min(i + batch_size, len(filtered))
        logger.info("Прогресс: %d/%d символов", done, len(filtered))

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
    setup_logging("logs/screener_zakonomer.log", "INFO")
    env_cfg, scan_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер запущен: timeframe=%s, interval=%ss, "
        "min_turnover=$%sM, lookback=%d days",
        env_cfg["timeframe"],
        env_cfg["scan_interval"],
        env_cfg["min_turnover_24h"] / 1_000_000,
        env_cfg["lookback_days"],
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
