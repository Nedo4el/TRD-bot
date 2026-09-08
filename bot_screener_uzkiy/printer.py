"""Вывод результатов скринера."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from tabulate import tabulate

from bot_screener_uzkiy.scanner import ScanResult


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_results(
    results: list[ScanResult],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    clear_console()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print("=" * 85)
    print("  UZKIY SCREENER — Quiet Zone")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 85)
    print()

    if not results:
        print("  No signals found.")
        print()
        print(f"  Pairs filtered: {filtered_symbols}/{total_symbols}")
        print(f"  Scan time: {scan_time:.1f}s")
        print("=" * 85)
        return

    rows = []
    for rank, r in enumerate(results, 1):
        rows.append([
            rank,
            r.symbol,
            r.timeframe,
            f"{r.price:.6f}" if r.price < 0.01 else f"{r.price:.4f}",
            f"{r.avg_width_pct:.3f}%",
            f"{r.max_width_pct:.3f}%",
            f"{r.avg_delta:.0f}",
            f"{r.max_delta:.0f}",
            f"{r.quiet_candles}/{r.total_candles}",
            f"${r.turnover_24h / 1_000_000:.0f}M",
            r.status,
            r.signal_time,
        ])

    headers = ["#", "Coin", "TF", "Price", "Avg W%", "Max W%", "Avg D", "Max D", "Quiet", "Turnover", "Status", "Time"]

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Signals: {len(results)} | "
        f"Pairs: {filtered_symbols}/{total_symbols} | "
        f"Time: {scan_time:.1f}s"
    )
    print("=" * 85)
