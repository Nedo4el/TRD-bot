"""Красивый вывод результатов скринера в консоль."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from tabulate import tabulate

from bot_screener.scanner import ScanResult


def clear_console() -> None:
    """Очистить консоль (кроссплатформенно)."""
    os.system("cls" if os.name == "nt" else "clear")


def format_turnover(value: float) -> str:
    """Форматировать оборот: 5000000 -> $5.0M."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def format_signal(signal: str, score: int) -> str:
    """Форматировать сигнал с оценкой."""
    if not signal:
        return ""
    return f"{signal} ({score}/100)"


def print_results(
    results: list[ScanResult],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    """Вывести таблицу результатов в консоль.

    Args:
        results: результаты сканирования (только с сигналами).
        total_symbols: общее количество символов на бирже.
        filtered_symbols: количество символов после фильтрации по обороту.
        scan_time: время сканирования в секундах.
    """
    clear_console()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print("=" * 90)
    print("  КРИПТОСКРИНЕР — Точки прорыва")
    print(f"  Bybit USDT-M | Последнее обновление: {now}")
    print("=" * 90)
    print()

    if not results:
        print("  Сигналов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 90)
        return

    # Таблица
    headers = [
        "Монета",
        "Таймфрейм",
        "Цена",
        "Сигнал",
        "Объём",
        "BBW",
        "ATR",
        "ADX",
        "RSI",
        "Оборот",
    ]

    rows = []
    for r in results:
        rows.append(
            [
                r.symbol,
                r.timeframe,
                f"{r.price:.4f}",
                format_signal(r.signal, r.score),
                f"{r.volume_ratio:.1f}x",
                f"{r.bbw * 100:.2f}%",
                f"{r.atr_value:.4f}",
                f"{r.adx_value:.1f}",
                f"{r.rsi_value:.1f}",
                format_turnover(r.turnover_24h),
            ]
        )

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Сигналов: {len(results)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 90)
