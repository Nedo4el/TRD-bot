"""Вывод результатов скринера закономерностей."""

from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

from tabulate import tabulate

from bot_screener_zakonomer.scanner import ScanResult

MSK = timezone(timedelta(hours=3))


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_results(
    results: list[ScanResult],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    clear_console()

    now = datetime.now(MSK).strftime("%H:%M:%S MSK")

    print("=" * 90)
    print("  ZAKONOMER SCREENER — Volume Spike Time Patterns")
    print(f"  Bybit USDT-M Futures | {now}")
    print("=" * 90)
    print()

    if not results:
        print("  No patterns found.")
        print()
        print(f"  Pairs filtered: {filtered_symbols}/{total_symbols}")
        print(f"  Scan time: {scan_time:.1f}s")
        print("=" * 90)
        return

    for r in results:
        print(f"  {r.symbol}  |  Spikes: {r.spikes_found}/{r.total_candles}  |  Threshold: ${r.threshold_usd:,.0f}")
        print(f"  {'─' * 80}")

        # Топ часы
        if r.hourly_patterns:
            top_hours = r.hourly_patterns[:5]
            hours_str = "  ".join(f"{p.hour:02d}:00({p.count},{p.pct}%)" for p in top_hours)
            print(f"    Top Hours:  {hours_str}")

        # Топ минуты
        if r.minute_patterns:
            top_min = r.minute_patterns[:5]
            min_str = "  ".join(f":{p.minute:02d}({p.count})" for p in top_min)
            print(f"    Top Min:    {min_str}")

        # Дни недели
        if r.weekday_patterns:
            wd_str = "  ".join(f"{d[:3]}({c})" for d, c in r.weekday_patterns[:5])
            print(f"    Weekdays:   {wd_str}")

        # Топ-5 всплесков
        if r.top_spikes:
            print(f"    Top Spikes:")
            for s in r.top_spikes[:5]:
                dt = datetime.fromtimestamp(s.open_time / 1000, tz=MSK)
                print(f"      {dt.strftime('%Y-%m-%d %H:%M')} MSK  ${s.volume_usdt:,.0f}  price={s.price:.4f}")

        print()

    print(
        f"  Results: {len(results)} | "
        f"Pairs: {filtered_symbols}/{total_symbols} | "
        f"Time: {scan_time:.1f}s"
    )
    print("=" * 90)
