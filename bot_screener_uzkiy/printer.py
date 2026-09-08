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

    print("=" * 90)
    print("  UZKIY SCREENER — Узкий диапазон ( quiet zone )")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 90)
    print()

    if not results:
        print("  Сигналов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 90)
        return

    rows = []
    for rank, r in enumerate(results, 1):
        rows.append([
            rank,
            r.symbol,
            r.timeframe,
            f"{r.price:.6f}" if r.price < 0.01 else f"{r.price:.4f}",
            f"{r.avg_width_pct:.3f}%",
            f"{r.max_vol_ratio:.1f}x",
            f"{r.avg_vol_ratio:.1f}x",
            f"{r.max_delta_ratio:.1f}x",
            f"${r.turnover_24h / 1_000_000:.0f}M",
            r.status,
            r.signal_time,
        ])

    headers = ["#", "Монета", "TF", "Цена", "Avg W%", "Max V/S", "Avg V/S", "Max D/S", "Оборот", "Статус", "Время"]

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Сигналов: {len(results)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 90)
