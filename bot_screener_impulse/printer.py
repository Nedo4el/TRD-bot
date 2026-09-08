"""Вывод результатов скринера импульсов."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from tabulate import tabulate

from bot_screener_impulse.scanner import ImpulseSignal


def clear_console() -> None:
    """Очистить консоль."""
    os.system("cls" if os.name == "nt" else "clear")


def _direction_icon(direction: str) -> str:
    """Стрелка направления."""
    return "▲" if direction == "LONG" else "▼"


def _strength_label(volume_ratio: float) -> str:
    """Метка силы импульса."""
    if volume_ratio >= 10:
        return "🔥 ОЧЕНЬ СИЛЬНЫЙ"
    if volume_ratio >= 7:
        return "⚡ СИЛЬНЫЙ"
    if volume_ratio >= 5:
        return "📊 СРЕДНИЙ"
    return "💤 СЛАБЫЙ"


def print_results(
    results: list[ImpulseSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    """Вывести таблицу результатов."""
    clear_console()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print("=" * 120)
    print("  IMPULSE SCREENER — Тиковый объем + Дельта + Ширина свечи")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 120)
    print()

    if not results:
        print("  Импульсов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 120)
        return

    # Таблица
    rows = []
    for rank, r in enumerate(results, 1):
        arrow = _direction_icon(r.direction)
        strength = _strength_label(r.volume_ratio)

        rows.append(
            [
                rank,
                r.symbol,
                f"{r.price:.6f}" if r.price < 0.01 else f"{r.price:.4f}",
                f"{arrow} {r.direction}",
                f"{r.volume_ratio:.1f}x",
                f"{r.candle_width:.3f}%",
                f"{r.delta_ratio:.1f}x",
                "✓" if r.confirmed else "✗",
                f"${r.turnover_24h / 1_000_000:.0f}M",
                strength,
                r.signal_time,
            ]
        )

    headers = [
        "#",
        "Монета",
        "Цена",
        "Направл.",
        "Volume",
        "Ширина",
        "Delta",
        "Подтв.",
        "Оборот",
        "Сила",
        "Время",
    ]

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Импульсов: {len(results)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 120)
