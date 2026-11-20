"""Вывод результатов Volume Profile скринера."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from tabulate import tabulate

from bot_screener_yrovni.scanner import VPSignal

MSK = timezone(timedelta(hours=3))

WINDOW_LABELS = {"all": "всё время"}


@dataclass
class _Row:
    """Строка таблицы: один уровень, окна объединены."""

    symbol: str
    price: float
    kind: str
    level: float
    distance: float
    time: str
    periods: set[str] = field(default_factory=set)
    volume: float = 0.0


def clear_console() -> None:
    """Очистить консоль."""
    os.system("cls" if os.name == "nt" else "clear")


def format_volume(value: float) -> str:
    """Форматировать оборот в USD."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}" if value > 0 else "—"


def _fmt_price(price: float) -> str:
    """Форматировать цену."""
    if price < 0.01:
        return f"{price:.6f}"
    if price < 1:
        return f"{price:.4f}"
    return f"{price:.2f}"


def _collect_rows(results: list[VPSignal]) -> list[_Row]:
    """Схлопнуть одинаковые уровни разных окон в одну строку."""
    grouped: dict[tuple[str, str, float], _Row] = {}

    for result in results:
        for level in result.levels:
            key = (result.symbol, level.kind, round(level.price, 8))
            row = grouped.get(key)
            if row is None:
                grouped[key] = _Row(
                    symbol=result.symbol,
                    price=result.current_price,
                    kind=level.kind,
                    level=level.price,
                    distance=level.distance_pct,
                    time=result.scan_time,
                    periods={level.period},
                    volume=level.volume,
                )
                continue
            row.periods.add(level.period)
            if abs(level.distance_pct) < abs(row.distance):
                row.distance = level.distance_pct
            row.volume = max(row.volume, level.volume)

    return list(grouped.values())


def _table(rows: list[_Row]) -> list[list[str]]:
    rows.sort(key=lambda row: abs(row.distance))
    table: list[list[str]] = []
    for row in rows:
        periods = ", ".join(WINDOW_LABELS.get(p, p) for p in sorted(row.periods))
        volume = format_volume(row.volume) if row.kind == "POC" else "—"
        table.append([
            row.symbol,
            _fmt_price(row.price),
            row.kind,
            periods,
            _fmt_price(row.level),
            volume,
            f"{row.distance:+.2f}%",
            row.time,
        ])
    return table


def print_results(
    results: list[VPSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    """Вывести таблицу результатов."""
    clear_console()

    now = datetime.now(MSK).strftime("%H:%M:%S MSK")

    print("=" * 118)
    print("  VOLUME PROFILE SCREENER — POC по окнам, экстремумы, ATH/ATL")
    print(f"  Bybit USDT-M | {now}")
    print("  POC — цена максимального оборота за окно | HIGH/LOW — экстремум окна")
    print("  ATH/ATL — абсолютный хай/лой истории (не POC) | Расст.: + сверху, - снизу")
    print("=" * 118)
    print()

    rows = _table(_collect_rows(results))

    if rows:
        headers = [
            "Монета",
            "Цена",
            "Тип",
            "Окна",
            "Уровень",
            "Оборот",
            "Расст. %",
            "Время",
        ]
        print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))
        print()
    else:
        print("  Сигналов не найдено.")
        print()

    print(
        f"  Уровней: {len(rows)} | Пар: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 118)
