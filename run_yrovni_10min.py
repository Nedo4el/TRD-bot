"""Запуск VP-скринера на 10 минут."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")
RUN_DURATION = 600  # 10 минут

from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

BOT_DIR = _PROJECT_ROOT / "bot_screener_yrovni"


def _load_config() -> dict:
    load_bot_env(BOT_DIR)
    raw_exclude = os.getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]
    return {
        "api_key": os.getenv("BYBIT_API_KEY", ""),
        "api_secret": os.getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "scan_interval": get_env_int("SCAN_INTERVAL", 300),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "num_bins": 100,
        "proximity_pct": get_env_float("PROXIMITY_PCT", 10.0),
        "exclude_symbols": exclude,
    }


def _generate_report(all_signals: list, scan_count: int, elapsed: float) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")
    filepath = REPORTS_DIR / f"yrovni_{now}.txt"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  VOLUME PROFILE SCREENER — POC + Дневные уровни\n")
        f.write(f"  Дата: {now} UTC | Сканирований: {scan_count} | Время: {elapsed:.0f} сек\n")
        f.write("=" * 80 + "\n\n")

        if not all_signals:
            f.write("  Сигналов не найдено.\n")
        else:
            by_symbol: dict = {}
            for sig in all_signals:
                if sig.symbol not in by_symbol:
                    by_symbol[sig.symbol] = sig

            for i, (symbol, sig) in enumerate(sorted(by_symbol.items(), key=lambda x: x[1].current_price), 1):
                f.write(f"  {i}. {symbol} — цена: {sig.current_price:.4f}\n")
                if sig.poc_levels:
                    f.write("     POC уровни:\n")
                    for lvl in sig.poc_levels:
                        f.write(f"       {lvl.period}: POC={lvl.poc_price:.4f} "
                                f"(объём={lvl.poc_volume:.0f}, расст={lvl.distance_pct}%)\n")
                if sig.daily_levels:
                    f.write("     Дневные уровни:\n")
                    for lvl in sig.daily_levels:
                        f.write(f"       {lvl.level_type}: {lvl.price:.4f} (расст={lvl.distance_pct}%)\n")
                f.write("\n")

        f.write("=" * 80 + "\n")

    logger.info("Отчёт сохранён: %s", filepath)


async def main() -> None:
    setup_logging("logs/yrovni_10min.log", "INFO")
    cfg = _load_config()
    metrics = ScreenerMetrics()

    logger.info("VP-скринер на 10 мин: interval=%ss, min_turnover=$%sM",
                cfg["scan_interval"], cfg["min_turnover_24h"] / 1_000_000)

    all_signals = []
    start = time.monotonic()
    scan_count = 0

    while time.monotonic() - start < RUN_DURATION:
        try:
            from bot_screener_klin.fetcher import Fetcher
            from bot_screener_yrovni.scanner import POC_PERIODS, scan_symbol

            fetcher = Fetcher(
                api_key=cfg["api_key"],
                api_secret=cfg["api_secret"],
                testnet=cfg["testnet"],
                metrics=metrics,
            )

            scan_start = time.monotonic()

            filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
            total_symbols = len(await fetcher.get_all_linear_symbols())
            exclude = set(cfg["exclude_symbols"])
            filtered = [(s, t) for s, t in filtered if s not in exclude]

            logger.info("Символов: %d (всего: %d)", len(filtered), total_symbols)

            results = []
            for symbol, _ in filtered:
                candles_by_period = {}
                for period, (interval, num_candles) in POC_PERIODS.items():
                    candles = await fetcher.get_klines(symbol=symbol, interval=interval, limit=num_candles)
                    if candles:
                        candles_by_period[period] = candles

                if not candles_by_period:
                    continue

                daily_candles = await fetcher.get_klines(symbol=symbol, interval="D", limit=5)
                if not daily_candles:
                    continue

                result = scan_symbol(
                    symbol=symbol,
                    candles_by_period=candles_by_period,
                    daily_candles=daily_candles,
                    num_bins=cfg["num_bins"],
                    proximity_pct=cfg["proximity_pct"],
                )

                if result.poc_levels or result.daily_levels:
                    results.append(result)
                    logger.info("СИГНАЛ: %s price=%.4f poc=%d daily=%d",
                                result.symbol, result.current_price,
                                len(result.poc_levels), len(result.daily_levels))
                    for lvl in result.daily_levels:
                        logger.info("  -> %s: %.4f (расст %.2f%%)", lvl.level_type, lvl.price, lvl.distance_pct)

            elapsed_scan = time.monotonic() - scan_start
            scan_count += 1
            all_signals.extend(results)
            metrics.record_scan(elapsed_scan, len(results), len(filtered))

            logger.info("Прон %d: %d сигналов (%.1f сек)", scan_count, len(results), elapsed_scan)

        except Exception:
            logger.exception("Ошибка сканирования")

        remaining = RUN_DURATION - (time.monotonic() - start)
        if remaining <= 0:
            break

        await asyncio.sleep(min(cfg["scan_interval"], remaining))

    total_elapsed = time.monotonic() - start
    if all_signals:
        _generate_report(all_signals, scan_count, total_elapsed)
    else:
        logger.info("Сигналов нет — отчёт не создаётся")
    logger.info("ГОТОВО! Сканирований: %d, сигналов: %d", scan_count, len(all_signals))


if __name__ == "__main__":
    asyncio.run(main())
