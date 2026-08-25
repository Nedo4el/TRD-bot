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
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": str(get_env_int("TIMEFRAME", 1)),
        "scan_interval": get_env_int("SCAN_INTERVAL", 60),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 5_000_000),
        "breakout_period": get_env_int("BREAKOUT_PERIOD", 20),
        "atr_buffer": get_env_float("ATR_BUFFER", 0.15),
        "volume_spike": get_env_float("VOLUME_SPIKE", 1.8),
        "volume_drop_before": get_env_float("VOLUME_DROP_BEFORE", 0.7),
        "bbw_threshold": get_env_float("BBW_THRESHOLD", 0.03),
        "adx_threshold": get_env_int("ADX_THRESHOLD", 25),
        "rsi_long": get_env_int("RSI_LONG", 55),
        "rsi_short": get_env_int("RSI_SHORT", 45),
        "ema_period": get_env_int("EMA_PERIOD", 20),
        "rsi_period": get_env_int("RSI_PERIOD", 14),
        "adx_period": get_env_int("ADX_PERIOD", 14),
        "atr_period": get_env_int("ATR_PERIOD", 14),
        "bb_period": get_env_int("BB_PERIOD", 20),
        "volume_ma_period": get_env_int("VOLUME_MA_PERIOD", 20),
        "api_delay_ms": get_env_int("API_DELAY_MS", 50),
        "kline_limit": get_env_int("KLINE_LIMIT", 100),
    }


async def scan_once(cfg: dict) -> None:
    """Один прогон сканирования."""
    from bot_screener.fetcher import Fetcher
    from bot_screener.printer import print_results
    from bot_screener.scanner import ScanResult, scan_symbol

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
    logger.info(
        "Найдено %d символов с оборотом > $%sM (всего: %d)",
        len(filtered),
        cfg["min_turnover_24h"] / 1_000_000,
        total_symbols,
    )

    # 2. Загружаем свечи и сканируем каждый символ
    results: list[ScanResult] = []

    for i, (symbol, turnover) in enumerate(filtered):
        candles = await fetcher.get_klines(
            symbol=symbol,
            interval=cfg["timeframe"],
            limit=cfg["kline_limit"],
        )

        if not candles:
            continue

        result = scan_symbol(
            symbol=symbol,
            timeframe=f"{cfg['timeframe']}m",
            candles=candles,
            turnover_24h=turnover,
            breakout_period=cfg["breakout_period"],
            atr_buffer=cfg["atr_buffer"],
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
            results.append(result)
            logger.info(
                "СИГНАЛ: %s %s score=%d price=%.4f vol=%.1fx",
                result.signal,
                result.symbol,
                result.score,
                result.price,
                result.volume_ratio,
            )

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
        "Скринер запущен: timeframe=%sm, interval=%ss, min_turnover=$%sM",
        cfg["timeframe"],
        cfg["scan_interval"],
        cfg["min_turnover_24h"] / 1_000_000,
    )

    while True:
        try:
            await scan_once(cfg)
        except Exception:
            logger.exception("Ошибка сканирования")

        logger.info("Следующее сканирование через %d сек...", cfg["scan_interval"])
        await asyncio.sleep(cfg["scan_interval"])


if __name__ == "__main__":
    asyncio.run(main())
