"""Screener — анализ спайков: прострел >=3% за <=20 секунд (LONG/SHORT).

Запуск:  python bot_screener_zakonomer/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_BOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_BOT_DIR))

from bot_screener_zakonomer.printer import print_results
from bot_screener_zakonomer.scanner import SpikeConfig, SymbolStats, analyze_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)


def _load_config() -> tuple[dict, SpikeConfig]:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

    exclude = [
        s.strip() for s in os.getenv("EXCLUDE_SYMBOLS", "").split(",") if s.strip()
    ]

    env_cfg: dict[str, Any] = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 3600),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_days": get_env_int("LOOKBACK_DAYS", 30),
        "report_file": os.getenv("REPORT_FILE", "reports/spike_scan_30d.md"),
    }

    scan_cfg = SpikeConfig(
        spike_pct=get_env_float("SPIKE_PCT", 0.03),
        window_sec=get_env_int("SPIKE_WINDOW_SEC", 20),
        cooldown_sec=get_env_int("SPIKE_COOLDOWN_SEC", 60),
        lookback_days=env_cfg["lookback_days"],
        interval=env_cfg["timeframe"],
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
    cfg: SpikeConfig,
) -> SymbolStats | None:
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
    return analyze_symbol(symbol, candles, cfg=cfg)


async def scan_once(
    env_cfg: dict,
    scan_cfg: SpikeConfig,
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

    exclude = set(scan_cfg.exclude_symbols)
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info(
        "Найдено %d символов с оборотом > $%sM (всего: %d, исключено: %d)",
        len(filtered),
        env_cfg["min_turnover_24h"] / 1_000_000,
        total_symbols,
        len(exclude),
    )

    now_ms = int(time.time() * 1000)
    lookback_ms = scan_cfg.lookback_days * 24 * 3600 * 1000
    start_ms = now_ms - lookback_ms

    results: list[SymbolStats] = []
    no_spikes = 0

    batch_size = 5
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(fetcher, sym, scan_cfg.interval, start_ms, now_ms, scan_cfg)
            for sym, _ in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for stats in batch_results:
            if stats is not None and stats.total > 0:
                results.append(stats)
            else:
                no_spikes += 1

        done = min(i + batch_size, len(filtered))
        logger.info("Прогресс: %d/%d символов", done, len(filtered))

    results.sort(key=lambda s: s.total, reverse=True)
    elapsed = time.monotonic() - start
    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        no_spikes=no_spikes,
        scan_time=elapsed,
        cfg=scan_cfg,
        report_file=env_cfg["report_file"],
    )


async def main() -> None:
    """Главный цикл скринера."""
    setup_logging("logs/screener_zakonomer.log", "INFO")
    env_cfg, scan_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер запущен: спайк %.0f%% за %dс, период %d дней, TF=%s, "
        "min_turnover=$%sM, interval=%dс",
        scan_cfg.spike_pct * 100,
        scan_cfg.window_sec,
        scan_cfg.lookback_days,
        scan_cfg.interval,
        scan_cfg.min_turnover_24h / 1_000_000,
        env_cfg["scan_interval"],
    )

    while True:
        try:
            await scan_once(env_cfg, scan_cfg, metrics)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        if metrics.scan_count % 5 == 0 and metrics.scan_count > 0:
            logger.info("МЕТРИКИ:\n%s", metrics.report())

        logger.info(
            "Следующее сканирование через %d сек...",
            env_cfg["scan_interval"],
        )
        await asyncio.sleep(env_cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
