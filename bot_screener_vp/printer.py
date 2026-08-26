"""Красивый вывод результатов Volume Profile скринера."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from tabulate import tabulate

from bot_screener_vp.scanner import VPSignal


def clear_console() -> None:
    """Очистить консоль."""
    os.system("cls" if os.name == "nt" else "clear")


def format_volume(value: float) -> str:
    """Форматировать объем."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def format_signal(signal: str) -> str:
    """Форматировать сигнал."""
    labels = {
        "BREAKOUT_UP": "ПРОБОЙ ВВЕРХ",
        "BREAKOUT_DOWN": "ПРОБОЙ ВНИЗ",
        "APPROACH_SUPPORT": "К СППОРТУ",
        "APPROACH_RESISTANCE": "К СОПРОТИВЛ.",
    }
    return labels.get(signal, signal)


def format_action(level_type: str, position: str) -> str:
    """Рекомендуемое действие."""
    if position == "BELOW" and level_type == "RESISTANCE":
        return "ЖДИ ПРОБОЙ ВВЕРХ"
    if position == "ABOVE" and level_type == "SUPPORT":
        return "ЖДИ ПРОБОЙ ВНИЗ"
    if position == "BELOW" and level_type == "SUPPORT":
        return "ОТСКОК ВВЕРХ"
    if position == "ABOVE" and level_type == "RESISTANCE":
        return "ОТСКОК ВНИЗ"
    return "НАБЛЮДЕНИЕ"


def print_results(
    results: list[VPSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    """Вывести таблицу результатов Volume Profile скринера.

    Args:
        results: результаты сканирования.
        total_symbols: общее количество символов.
        filtered_symbols: количество символов после фильтрации.
        scan_time: время сканирования в секундах.
    """
    clear_console()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print("=" * 120)
    print("  VOLUME PROFILE SCREENER — Уровни с максимальным объемом")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 120)
    print()

    # Собираем все уровни в один список для таблицы
    all_rows = []
    for result in results:
        for level in result.levels:
            all_rows.append(
                [
                    result.symbol,
                    result.timeframe,
                    f"{result.current_price:.4f}",
                    f"#{level.rank}",
                    f"{level.price:.4f}",
                    level.level_type,
                    f"{level.distance_pct}%",
                    format_volume(level.total_volume),
                    format_action(level.level_type, level.position),
                ]
            )

    if not all_rows:
        print("  Сигналов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 120)
        return

    # Сортируем по расстоянию (ближе = выше)
    all_rows.sort(key=lambda r: float(r[6].rstrip("%")))

    headers = [
        "Монета",
        "TF",
        "Цена",
        "Ранг",
        "Уровень",
        "Тип",
        "Расст.",
        "Объем",
        "Действие",
    ]

    print(tabulate(all_rows, headers=headers, tablefmt="simple", stralign="right"))

    print()
    print(
        f"  Сигналов: {len(all_rows)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 120)
