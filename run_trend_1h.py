"""Запуск скринера тренда на 1 час + отчёт.

Запуск: python run_trend_1h.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_MSK = timezone(timedelta(hours=3))
_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")
RUN_DURATION = 3600

from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

_TREND_DIR = _PROJECT_ROOT / "bot_screener_trend"

_SCREENING_ENV_KEYS = [
    "BYBIT_API_KEY", "BYBIT_API_SECRET", "TESTNET", "TIMEFRAME",
    "SCAN_INTERVAL", "MIN_TURNOVER_24H", "LOOKBACK_BARS",
    "EXCLUDE_SYMBOLS", "SWING_WINDOW", "EMA_FAST", "EMA_SLOW",
    "ADX_MIN", "MIN_SWINGS",
]


def _clear_screener_env() -> None:
    for key in _SCREENING_ENV_KEYS:
        os.environ.pop(key, None)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _load_trend_config() -> tuple[dict, object]:
    _clear_screener_env()
    _load_dotenv(_TREND_DIR / ".env")
    load_bot_env(_TREND_DIR)

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

    from bot_screener_trend.scanner import ScanConfig
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


async def _run_trend_once(env_cfg: dict, scan_cfg: object, metrics: ScreenerMetrics) -> None:
    from bot_screener_trend.fetcher import Fetcher
    from bot_screener_trend.main import _scan_one
    from bot_screener_trend.printer import print_results, save_report

    fetcher = Fetcher(
        api_key=env_cfg["api_key"],
        api_secret=env_cfg["api_secret"],
        testnet=env_cfg["testnet"],
        metrics=metrics,
    )

    start = time.monotonic()

    logger.info("[TREND] Получаю список символов...")
    filtered = await fetcher.get_filtered_symbols(env_cfg["min_turnover_24h"])
    total_symbols = len(await fetcher.get_all_linear_symbols())

    exclude = set(env_cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info("[TREND] Найдено %d символов", len(filtered))

    results = []
    batch_size = 5
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i: i + batch_size]
        tasks = [
            _scan_one(fetcher, sym, env_cfg["timeframe"], env_cfg["lookback_bars"], scan_cfg, turnover)
            for sym, turnover in batch
        ]
        batch_results = await asyncio.gather(*tasks)
        for r in batch_results:
            if r:
                results.append(r)
        done = min(i + batch_size, len(filtered))
        logger.info("[TREND] Прогресс: %d/%d", done, len(filtered))

    elapsed = time.monotonic() - start
    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(results, total_symbols, len(filtered), elapsed)

    if REPORTS_DIR.exists():
        path = save_report(results, total_symbols, len(filtered), elapsed, str(REPORTS_DIR))
        logger.info("[TREND] Отчёт: %s", path)


async def main() -> None:
    setup_logging("logs/run_trend_1h.log", "INFO")
    logger.info("Запуск скринера тренда на %d сек...", RUN_DURATION)

    end_time = time.monotonic() + RUN_DURATION
    scan_count = 0

    while time.monotonic() < end_time:
        env_cfg, scan_cfg = _load_trend_config()
        metrics = ScreenerMetrics()

        try:
            await _run_trend_once(env_cfg, scan_cfg, metrics)
            scan_count += 1
        except Exception:
            logger.exception("[TREND] Ошибка сканирования")

        remaining = end_time - time.monotonic()
        if remaining <= 0:
            break

        sleep_time = min(env_cfg["scan_interval"], remaining)
        logger.info("[TREND] Следующее через %d сек (осталось %.0f сек)", sleep_time, remaining)
        await asyncio.sleep(sleep_time)

    logger.info("Завершено. Выполнено сканов: %d", scan_count)


if __name__ == "__main__":
    asyncio.run(main())
