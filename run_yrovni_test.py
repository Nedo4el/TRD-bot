"""Тест: собрать 10 сигналов по дневным уровням (High/Low по UTC)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")
TARGET_SIGNALS = 10

from core.config import get_env_bool, get_env_float, load_bot_env
from core.logger import get_logger, setup_logging

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
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 50_000_000),
        "num_bins": 100,
        "proximity_pct": 15.0,
        "exclude_symbols": exclude,
    }


def _generate_report(signals: list) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")
    filepath = REPORTS_DIR / f"yrovni_test_{now}.txt"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  VOLUME PROFILE — ТЕСТ ДНЕВНЫХ УРОВНЕЙ (High/Low по UTC 03:00 МСК)\n")
        f.write(f"  Дата: {now} UTC | Сигналов: {len(signals)}\n")
        f.write("=" * 80 + "\n\n")

        for i, sig in enumerate(signals, 1):
            f.write(f"  {i}. {sig.symbol} — цена: {sig.current_price:.4f}\n")
            if sig.poc_levels:
                f.write("     POC:\n")
                for lvl in sig.poc_levels:
                    f.write(f"       {lvl.period}: {lvl.poc_price:.4f} "
                            f"(объём={lvl.poc_volume:.0f}, расст={lvl.distance_pct}%)\n")
            if sig.daily_levels:
                f.write("     Дневные (High/Low вчера по UTC):\n")
                for lvl in sig.daily_levels:
                    f.write(f"       {lvl.level_type}: {lvl.price:.4f} (расст={lvl.distance_pct}%)\n")
            f.write("\n")

        f.write("=" * 80 + "\n")

    logger.info("Отчёт: %s", filepath)


async def main() -> None:
    setup_logging("logs/yrovni_test.log", "INFO")
    cfg = _load_config()

    from bot_screener_klin.fetcher import Fetcher
    from bot_screener_yrovni.scanner import scan_symbol

    fetcher = Fetcher(
        api_key=cfg["api_key"],
        api_secret=cfg["api_secret"],
        testnet=cfg["testnet"],
    )

    logger.info("Собираю %d сигналов по дневным уровням (High/Low)...", TARGET_SIGNALS)

    filtered = await fetcher.get_filtered_symbols(cfg["min_turnover_24h"])
    exclude = set(cfg["exclude_symbols"])
    filtered = [(s, t) for s, t in filtered if s not in exclude]

    logger.info("Символов: %d", len(filtered))

    signals = []
    for symbol, _ in filtered:
        if len(signals) >= TARGET_SIGNALS:
            break

        # Загружаем 1H свечи (48 шт) — достаточно для сборки 2 дневных свечей
        hourly_candles = await fetcher.get_klines(symbol=symbol, interval="60", limit=48)
        if not hourly_candles or len(hourly_candles) < 24:
            logger.info("%s: пропуск (hourly=%d)", symbol, len(hourly_candles) if hourly_candles else 0)
            await asyncio.sleep(1.5)
            continue

        # POC из тех же 1H свечей
        candles_by_period = {
            "24h": hourly_candles[-24:],
            "12h": hourly_candles[-12:],
        }

        result = scan_symbol(
            symbol=symbol,
            candles_by_period=candles_by_period,
            hourly_candles=hourly_candles,
            num_bins=cfg["num_bins"],
            proximity_pct=cfg["proximity_pct"],
        )

        if result.daily_levels:
            signals.append(result)
            logger.info(
                "[%d/%d] %s price=%.4f daily=%d",
                len(signals), TARGET_SIGNALS, result.symbol, result.current_price,
                len(result.daily_levels),
            )
            for lvl in result.daily_levels:
                logger.info("  -> %s: %.4f (расст %.2f%%)", lvl.level_type, lvl.price, lvl.distance_pct)
            if result.poc_levels:
                for lvl in result.poc_levels:
                    logger.info("  -> POC %s: %.4f (расст %.2f%%)", lvl.period, lvl.poc_price, lvl.distance_pct)
        else:
            logger.info("%s: price=%.4f — дневные уровни вне %.1f%%", symbol, result.current_price, cfg["proximity_pct"])

        await asyncio.sleep(1.5)

    if not signals:
        logger.info("Сигналов нет — отчёт не создаётся")
        return

    _generate_report(signals)
    logger.info("ГОТОВО! Сигналов: %d", len(signals))


if __name__ == "__main__":
    asyncio.run(main())
