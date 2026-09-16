"""Скринер импульсов: свеча с движением >= N%.

Запуск:  py bot_screener_impulse/main.py
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

from bot_screener_impulse.scanner import ImpulseConfig, ImpulseSignal, analyze_symbol
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)


def _load_config() -> tuple[dict, ImpulseConfig]:
    load_bot_env(_BOT_DIR)

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": os.getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 15),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 100),
        "exclude_symbols": exclude,
    }

    impulse_cfg = ImpulseConfig(
        min_move_pct=get_env_float("MIN_MOVE_PCT", 4.0),
        confirmation_candles=get_env_int("CONFIRMATION_CANDLES", 2),
    )

    return env_cfg, impulse_cfg


async def _scan_one(
    fetcher: object,
    symbol: str,
    timeframe: str,
    lookback: int,
    impulse_cfg: ImpulseConfig,
    turnover_24h: float,
) -> ImpulseSignal | None:
    from bot_screener_klin.fetcher import Fetcher

    assert isinstance(fetcher, Fetcher)

    candles = await fetcher.get_klines(
        symbol=symbol,
        interval=timeframe,
        limit=lookback,
    )

    if not candles:
        return None

    return analyze_symbol(
        symbol=symbol,
        timeframe=timeframe,
        candles=candles,
        cfg=impulse_cfg,
        turnover_24h=turnover_24h,
    )


async def scan_once(cfg: dict, impulse_cfg: ImpulseConfig, metrics: ScreenerMetrics) -> None:
    from bot_screener_impulse.printer import print_results
    from bot_screener_klin.fetcher import Fetcher

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

    tickers_map: dict[str, dict] = {}
    tickers = await fetcher.get_linear_tickers()
    for t in tickers:
        tickers_map[t.get("symbol", "")] = t

    results = []

    batch_size = 10
    for i in range(0, len(filtered), batch_size):
        batch = filtered[i : i + batch_size]
        tasks = [
            _scan_one(
                fetcher,
                sym,
                cfg["timeframe"],
                cfg["lookback_bars"],
                impulse_cfg,
                _get_turnover(tickers_map, sym),
            )
            for sym, _ in batch
        ]
        batch_results = await asyncio.gather(*tasks)

        for result in batch_results:
            if result is not None:
                logger.info(
                    "ИМПУЛЬС: %s %s move=%.2f%%",
                    result.symbol,
                    result.direction,
                    result.move_pct,
                )
                results.append(result)

    results.sort(key=lambda r: r.move_pct, reverse=True)
    elapsed = time.monotonic() - start

    metrics.record_scan(elapsed, len(results), len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
    )


def _get_turnover(tickers_map: dict[str, dict], symbol: str) -> float:
    t = tickers_map.get(symbol, {})
    vol = t.get("turnover24h", "0")
    try:
        return float(vol)
    except (ValueError, TypeError):
        return 0.0


async def main() -> None:
    setup_logging("logs/screener_impulse.log", "INFO")
    env_cfg, impulse_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер импульсов: timeframe=%s, interval=%ss, min_move=%.1f%%, min_turnover=$%sM",
        env_cfg["timeframe"],
        env_cfg["scan_interval"],
        impulse_cfg.min_move_pct,
        env_cfg["min_turnover_24h"] / 1_000_000,
    )

    while True:
        try:
            await scan_once(env_cfg, impulse_cfg, metrics)
        except (OSError, ValueError):
            logger.exception("Ошибка сканирования")

        if metrics.scan_count % 10 == 0 and metrics.scan_count > 0:
            logger.info("МЕТРИКИ:\n%s", metrics.report())

        logger.info("Следующее сканирование через %d сек...", env_cfg["scan_interval"])
        await asyncio.sleep(env_cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
