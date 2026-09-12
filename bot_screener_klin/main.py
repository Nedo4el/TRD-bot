"""Скринер сужения диапазона — скальпинг Bybit USDT-M фьючерсы.

Запуск:  python bot_screener_klin/main.py
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

from bot_screener_klin.fetcher import Fetcher
from bot_screener_klin.scanner import ScanResult
from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)


def _load_config() -> dict:
    """Загрузить конфигурацию из .env."""
    load_bot_env(_BOT_DIR)

    raw_timeframes = os.getenv("TIMEFRAMES", "1,3,5")
    timeframes = [t.strip() for t in raw_timeframes.split(",") if t.strip()]

    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframes": timeframes,
        "scan_interval": get_env_int("SCAN_INTERVAL", 10),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 10_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 50),
        "spread_max_pct": get_env_float("SPREAD_MAX_PCT", 0.05),
        "min_score": get_env_int("MIN_SCORE", 40),
        "max_price": get_env_float("MAX_PRICE", 0),
        "exclude_symbols": exclude,
    }

    return env_cfg


async def _scan_symbol(
    fetcher: Fetcher,
    symbol: str,
    turnover: float,
    timeframe: str,
    env_cfg: dict,
    spread_cache: dict[str, float],
) -> ScanResult | None:
    """Просканировать один символ на одном таймфрейме."""
    from bot_screener_klin.scanner import scan_symbol as _scan_symbol_fn

    candles = await fetcher.get_klines(
        symbol=symbol,
        interval=timeframe,
        limit=env_cfg["lookback_bars"],
    )

    if not candles:
        return None

    max_price = env_cfg.get("max_price", 0)
    if max_price > 0 and candles[-1]["close"] > max_price:
        return None

    spread = spread_cache.get(symbol, 0.0)
    if spread == 0.0:
        spread = await fetcher.get_spread(symbol)
        spread_cache[symbol] = spread

    if spread > env_cfg["spread_max_pct"]:
        return None

    result = _scan_symbol_fn(
        symbol=symbol,
        timeframe=f"{timeframe}m",
        candles=candles,
        turnover_24h=turnover,
        spread_pct=spread,
    )

    if result.squeeze_type and result.score >= env_cfg["min_score"]:
        logger.info(
            "СИГНАЛ: %s %s [%s] score=%d price=%.4f ADX=%.1f",
            result.direction or "?",
            result.symbol,
            result.squeeze_type.value,
            result.score,
            result.price,
            result.adx_value,
        )

    return result


async def scan_once(env_cfg: dict, metrics: ScreenerMetrics) -> None:
    """Один прогон сканирования."""
    from bot_screener_klin.printer import print_results

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

    best: dict[str, ScanResult] = {}
    spread_cache: dict[str, float] = {}

    for timeframe in env_cfg["timeframes"]:
        logger.info("Сканирую %sm...", timeframe)

        batch_size = 10
        for i in range(0, len(filtered), batch_size):
            batch = filtered[i : i + batch_size]
            tasks = [
                _scan_symbol(
                    fetcher, sym, tv, timeframe, env_cfg, spread_cache
                )
                for sym, tv in batch
            ]
            batch_results = await asyncio.gather(*tasks)

            for result in batch_results:
                if result and result.squeeze_type:
                    prev = best.get(result.symbol)
                    if prev is None or result.score > prev.score:
                        best[result.symbol] = result

    results = sorted(best.values(), key=lambda r: r.score, reverse=True)
    elapsed = time.monotonic() - start

    signals_found = len([r for r in results if r.score >= env_cfg["min_score"]])
    metrics.record_scan(elapsed, signals_found, len(filtered))

    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
        min_score=env_cfg["min_score"],
    )


async def main() -> None:
    """Главный цикл скринера."""
    setup_logging("logs/screener_uzkiy.log", "INFO")
    env_cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info(
        "Скринер запущен: timeframes=%s, interval=%ss, min_turnover=$%sM",
        ",".join(env_cfg["timeframes"]),
        env_cfg["scan_interval"],
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
