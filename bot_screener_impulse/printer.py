"""Вывод результатов скринера импульсов."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

from tabulate import tabulate

from bot_screener_impulse.scanner import ImpulseSignal


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _direction_icon(direction: str) -> str:
    return "▲" if direction == "LONG" else "▼"


def _strength_label(move_pct: float) -> str:
    if move_pct >= 10:
        return "🔥 ОЧЕНЬ СИЛЬНЫЙ"
    if move_pct >= 7:
        return "⚡ СИЛЬНЫЙ"
    if move_pct >= 5:
        return "📊 СРЕДНИЙ"
    return "💤 СЛАБЫЙ"


def print_results(
    results: list[ImpulseSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    clear_console()

    now = datetime.now(MSK).strftime("%H:%M:%S MSK")

    print("=" * 90)
    print("  IMPULSE SCREENER — Свечи с движением >= N%")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 90)
    print()

    if not results:
        print("  Импульсов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 90)
        return

    rows = []
    for rank, r in enumerate(results, 1):
        arrow = _direction_icon(r.direction)
        strength = _strength_label(r.move_pct)

        rows.append([
            rank,
            r.symbol,
            f"{r.price:.6f}" if r.price < 0.01 else f"{r.price:.4f}",
            f"{arrow} {r.direction}",
            f"{r.move_pct:.2f}%",
            strength,
            f"${r.turnover_24h / 1_000_000:.0f}M",
            r.signal_time,
        ])

    headers = ["#", "Монета", "Цена", "Направл.", "Движение", "Сила", "Оборот", "Время"]

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Импульсов: {len(results)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 90)
