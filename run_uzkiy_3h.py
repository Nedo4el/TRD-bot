"""Запуск uzkiy скринера на 3 часа + отчёт с таймстампами.

Запуск: python run_uzkiy_3h.py
Остановка: Ctrl+C
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

REPORTS_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")
RUN_DURATION = 3 * 3600  # 3 часа

from core.config import get_env_bool, get_env_float, get_env_int, load_bot_env
from core.logger import get_logger, setup_logging
from core.metrics import ScreenerMetrics

logger = get_logger(__name__)

UZKIY_DIR = _PROJECT_ROOT / "bot_screener_uzkiy"


def _load_config() -> tuple[dict, object]:
    load_bot_env(UZKIY_DIR)
    from bot_screener_uzkiy.scanner import ScanConfig

    raw_exclude = __import__("os").getenv("EXCLUDE_SYMBOLS", "BTCUSDT,ETHUSDT")
    exclude = [s.strip() for s in raw_exclude.split(",") if s.strip()]

    env_cfg = {
        "api_key": __import__("os").getenv("BYBIT_API_KEY", ""),
        "api_secret": __import__("os").getenv("BYBIT_API_SECRET", ""),
        "testnet": get_env_bool("TESTNET", True),
        "timeframe": __import__("os").getenv("TIMEFRAME", "1"),
        "scan_interval": get_env_int("SCAN_INTERVAL", 60),
        "min_turnover_24h": get_env_float("MIN_TURNOVER_24H", 30_000_000),
        "lookback_bars": get_env_int("LOOKBACK_BARS", 60),
        "exclude_symbols": exclude,
    }

    return env_cfg, ScanConfig()


async def main() -> None:
    setup_logging("logs/run_uzkiy_3h.log", "INFO")
    logger.info("=" * 60)
    logger.info("ЗАПУСК UZKIY НА 3 ЧАСА")
    logger.info("Отчёты: %s", REPORTS_DIR)
    logger.info("=" * 60)

    from bot_screener_uzkiy.main import scan_once
    from bot_screener_uzkiy.scanner import ScanResult

    env_cfg, scan_cfg = _load_config()
    metrics = ScreenerMetrics()
    all_signals: list[tuple[str, ScanResult]] = []  # (timestamp, result)

    start = time.monotonic()
    scan_num = 0

    while time.monotonic() - start < RUN_DURATION:
        scan_num += 1
        elapsed = time.monotonic() - start
        remaining = RUN_DURATION - elapsed
        ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")

        logger.info("[Scan #%d] %s | Осталось: %.0f мин", scan_num, ts, remaining / 60)

        try:
            import bot_screener_uzkiy.printer as printer_mod
            import bot_screener_uzkiy.main as main_mod

            original_print = printer_mod.print_results
            captured: dict = {"results": [], "meta": {}}

            def _capture_print(results, **kwargs):
                captured["results"] = list(results)
                captured["meta"] = kwargs

            printer_mod.print_results = _capture_print
            main_mod.print_results = _capture_print
            try:
                await scan_once(env_cfg, scan_cfg, metrics)
            finally:
                printer_mod.print_results = original_print
                main_mod.print_results = original_print

            if captured["results"]:
                signal_ts = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")
                for r in captured["results"]:
                    all_signals.append((signal_ts, r))
                    logger.info("  -> %s %s [quiet=%d/%d] %s",
                                r.symbol, r.status, r.quiet_candles,
                                r.total_candles, signal_ts)

        except Exception:
            logger.exception("[Scan #%d] Ошибка", scan_num)

        if remaining > 0:
            await asyncio.sleep(min(env_cfg["scan_interval"], remaining))

    # --- Отчёт ---
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")
    report_path = REPORTS_DIR / f"uzkiy_3h_{now}.txt"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("  UZKIY SCREENER — 3 HOUR REPORT\n")
        f.write(f"  Generated: {now} MSK\n")
        f.write(f"  Scans: {scan_num} | Total signals: {len(all_signals)}\n")
        f.write("=" * 80 + "\n\n")

        if not all_signals:
            f.write("  No signals found.\n")
        else:
            # Группировка по монете
            by_coin: dict[str, list[tuple[str, ScanResult]]] = {}
            for ts, sig in all_signals:
                by_coin.setdefault(sig.symbol, []).append((ts, sig))

            for symbol, entries in sorted(by_coin.items()):
                f.write(f"  {symbol}\n")
                f.write(f"  {'─' * 60}\n")
                for ts, sig in entries:
                    f.write(f"    [{ts}] {sig.timeframe} | "
                            f"Price: {sig.price:.6f} | "
                            f"AvgW: {sig.avg_width_pct:.3f}% | "
                            f"MaxW: {sig.max_width_pct:.3f}% | "
                            f"Quiet: {sig.quiet_candles}/{sig.total_candles} | "
                            f"Turnover: ${sig.turnover_24h / 1_000_000:.0f}M | "
                            f"{sig.status}\n")
                f.write(f"    Total signals: {len(entries)}\n\n")

        f.write("=" * 80 + "\n")

    logger.info("Отчёт сохранён: %s", report_path)
    logger.info("ГОТОВО! Всего сигналов: %d", len(all_signals))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
