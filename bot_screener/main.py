"""Скринер точек прорыва — Bybit USDT-M фьючерсы.

Запуск:  python bot_screener/main.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Корень проекта для core/
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

    raw_timeframes = os.getenv("TIMEFRAMES", "1,5")
    timeframes = [t.strip() for t in raw_timeframes.split(",") if t.strip()]

    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframes": timeframes,
        "scan_interval": get_env_int("SCAN_INTERVAL", 60),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 5_000_000),
        "consolidation_period": get_env_int("CONSOLIDATION_PERIOD", 10),
        "consolidation_range_pct": get_env_float("CONSOLIDATION_RANGE_PCT", 0.015),
        "volume_spike": get_env_float("VOLUME_SPIKE", 1.3),
        "volume_drop_before": get_env_float("VOLUME_DROP_BEFORE", 0.8),
        "bbw_threshold": get_env_float("BBW_THRESHOLD", 0.05),
        "adx_threshold": get_env_int("ADX_THRESHOLD", 15),
        "rsi_long": get_env_int("RSI_LONG", 50),
        "rsi_short": get_env_int("RSI_SHORT", 50),
        "ema_period": get_env_int("EMA_PERIOD", 20),
        "rsi_period": get_env_int("RSI_PERIOD", 14),
        "adx_period": get_env_int("ADX_PERIOD", 14),
        "atr_period": get_env_int("ATR_PERIOD", 14),
        "bb_period": get_env_int("BB_PERIOD", 20),
        "volume_ma_period": get_env_int("VOLUME_MA_PERIOD", 20),
        "api_delay_ms": get_env_int("API_DELAY_MS", 50),
        "kline_limit": get_env_int("KLINE_LIMIT", 100),
        "exclude_symbols": [
            "BTCUSDT",
            "ETHUSDT",
            "BNBUSDT",
            "SOLUSDT",
            "XRPUSDT",
            "DOGEUSDT",
            "ADAUSDT",
            "AVAXUSDT",
            "DOTUSDT",
            "LINKUSDT",
        ],
    }


async def _scan_symbol(
    fetcher: object,
    symbol: str,
    turnover: float,
    timeframe: str,
    cfg: dict,
    seen: dict[str, object],
) -> object | None:
    """Просканировать один символ на одном таймфрейме."""
    from bot_screener.scanner import scan_symbol

    cache_key = f"{symbol}_{timeframe}"
    if cache_key in seen:
        return None

    candles = await fetcher.get_klines(  # type: ignore[union-attr]
        symbol=symbol,
        interval=timeframe,
        limit=cfg["kline_limit"],
    )

    if not candles:
        return None

    result = scan_symbol(
        symbol=symbol,
        timeframe=f"{timeframe}m",
        candles=candles,
        turnover_24h=turnover,
        consolidation_period=cfg["consolidation_period"],
        consolidation_range_pct=cfg["consolidation_range_pct"],
        volume_spike=cfg["volume_spike"],
        volume_drop_before=cfg["volume_drop_before"],
        bbw_threshold=cfg["bbw_threshold"],
        adx_threshold=cfg["adx_threshold"],
        rsi_long=cfg["rsi_long"],
        rsi_short=cfg["rsi_short"],
        ema_period=cfg["ema_period"],
        rsi_period=cfg["rsi_period"],
        adx_period=cfg["adx_period"],
        atr_period=cfg["atr_period"],
        bb_period=cfg["bb_period"],
        volume_ma_period=cfg["volume_ma_period"],
    )

    if result.signal:
        logger.info(
            "СИГНАЛ: %s %s [%s] score=%d price=%.4f vol=%.1fx",
            result.signal,
            result.symbol,
            result.timeframe,
            result.score,
            result.price,
            result.volume_ratio,
        )

    return result


async def scan_once(cfg: dict) -> None:
    """Один прогон сканирования по всем таймфреймам."""
    from bot_screener.fetcher import Fetcher
    from bot_screener.printer import print_results
    from bot_screener.scanner import ScanResult

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
    )

    start = time.monotonic()

    # 1. Получаем символы с оборотом > порога
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

    # 2. Сканируем каждый символ на каждом таймфрейме
    results: list[ScanResult] = []
    seen: dict[str, ScanResult] = {}  # symbol -> лучший результат

    for timeframe in cfg["timeframes"]:
        logger.info("Сканирую таймфрейм %sm...", timeframe)

        for symbol, turnover in filtered:
            result = await _scan_symbol(fetcher, symbol, turnover, timeframe, cfg, seen)

            if result and result.signal:
                prev = seen.get(result.symbol)
                if prev is None or result.score > prev.score:
                    seen[result.symbol] = result

    results = sorted(seen.values(), key=lambda r: r.score, reverse=True)

    elapsed = time.monotonic() - start

    # 3. Вывод результатов
    print_results(
        results=results,
        total_symbols=total_symbols,
        filtered_symbols=len(filtered),
        scan_time=elapsed,
    )


async def main() -> None:
    """Главный цикл скринера."""
    setup_logging("logs/screener.log", "INFO")
    cfg = _load_config()

    logger.info(
        "Скринер запущен: timeframes=%s, interval=%ss, min_turnover=$%sM",
        ",".join(cfg["timeframes"]),
        cfg["scan_interval"],
        cfg["min_turnover_24h"] / 1_000_000,
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
