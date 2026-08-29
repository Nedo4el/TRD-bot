"""Красивый вывод результатов скринера сужения в консоль."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from tabulate import tabulate

from bot_screener_uzkiy.scanner import ScanResult


def clear_console() -> None:
    """Очистить консоль."""
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


def print_results(
    results: list[ScanResult],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
    min_score: int = 50,
) -> None:
    """Вывести таблицу результатов.

    Args:
        results: результаты сканирования.
        total_symbols: общее количество символов.
        filtered_symbols: символов после фильтра.
        scan_time: время сканирования в секундах.
        min_score: минимальный скор для отображения.
    """
    filtered = [r for r in results if r.score >= min_score]
    clear_console()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print("=" * 120)
    print("  СКРИНЕР СУЖЕНИЯ ДИАПАЗОНА — Скальпинг")
    print(f"  Bybit USDT-M | {now} | {scan_time:.1f} сек")
    print("=" * 120)
    print()

    if not filtered:
        print("  Сигналов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print("=" * 120)
        return

    headers = [
        "Монета",
        "TF",
        "Цена",
        "Тип сужения",
        "ATR",
        "ATR%",
        "BB%",
        "ADX",
        "Направление",
        "SL",
        "TP",
        "Спред",
        "Оборот",
        "Score",
    ]

    rows = []
    for r in filtered:
        squeeze_name = r.squeeze_type.value if r.squeeze_type else "-"
        direction = r.direction if r.direction else "-"
        atr_sl = f"{r.stop_loss:.4f}" if r.stop_loss > 0 else "-"
        tp = f"{r.take_profit:.4f}" if r.take_profit > 0 else "-"
        spread = f"{r.spread_pct:.3f}%" if r.spread_pct > 0 else "-"

        rows.append(
            [
                r.symbol,
                r.timeframe,
                f"{r.price:.4f}",
                squeeze_name,
                f"{r.atr_current:.4f}",
                f"{r.atr_percent:.2f}%",
                f"{r.bb_width:.2f}%",
                f"{r.adx_value:.1f}",
                direction,
                atr_sl,
                tp,
                spread,
                format_turnover(r.turnover_24h),
                r.score,
            ]
        )

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Сигналов: {len(filtered)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 120)
