"""Вывод результатов Volume Profile скринера."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

from tabulate import tabulate

from bot_screener_yrovni.scanner import VPSignal


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


def _fmt_price(price: float) -> str:
    """Форматировать цену."""
    if price < 0.01:
        return f"{price:.6f}"
    if price < 1:
        return f"{price:.4f}"
    return f"{price:.2f}"


def _dedup_poc(results: list[VPSignal]) -> list[dict]:
    """Дедупликация POC: одинаковые цены → один ряд с несколькими периодами."""
    # {symbol: {poc_price: {"periods": [], "volume": float, "distance": float, "time": str}}}
    by_symbol: dict[str, dict[float, dict]] = {}

    for result in results:
        if result.symbol not in by_symbol:
            by_symbol[result.symbol] = {}

        poc_map = by_symbol[result.symbol]

        for level in result.poc_levels:
            # Округляем цену для сравнения (6 знаков)
            key = round(level.poc_price, 6)

            if key in poc_map:
                # Уже есть — добавляем период
                if level.period not in poc_map[key]["periods"]:
                    poc_map[key]["periods"].append(level.period)
            else:
                poc_map[key] = {
                    "periods": [level.period],
                    "price": result.current_price,
                    "poc_price": level.poc_price,
                    "volume": level.poc_volume,
                    "distance": level.distance_pct,
                    "time": result.scan_time,
                }

    # Собираем в список
    rows = []
    for symbol, poc_map in by_symbol.items():
        for poc_data in poc_map.values():
            periods_str = ", ".join(sorted(poc_data["periods"]))
            rows.append([
                symbol,
                _fmt_price(poc_data["price"]),
                periods_str,
                _fmt_price(poc_data["poc_price"]),
                format_volume(poc_data["volume"]),
                f"{poc_data['distance']}%",
                poc_data["time"],
            ])

    return rows


def print_results(
    results: list[VPSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
) -> None:
    """Вывести таблицу результатов."""
    clear_console()

    now = datetime.now(MSK).strftime("%H:%M:%S MSK")

    print("=" * 100)
    print("  VOLUME PROFILE SCREENER — POC + Дневные уровни")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 100)
    print()

    if not results:
        print("  Сигналов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 100)
        return

    # === Таблица 1: POC уровни (с дедупликацией) ===
    poc_rows = _dedup_poc(results)

    if poc_rows:
        poc_rows.sort(key=lambda r: float(r[5].rstrip("%")))

        print("  === POC УРОВНИ (макс. объем по цене) ===")
        print()

        headers = ["Монета", "Цена", "Периоды", "POC", "Объем", "Расст. %", "Время"]
        print(tabulate(poc_rows, headers=headers, tablefmt="simple", stralign="right"))
        print()

    # === Таблица 2: Дневные уровни ===
    daily_rows = []
    for result in results:
        for level in result.daily_levels:
            label = "ОТКРЫТИЕ" if level.level_type == "OPEN" else "ЗАКРЫТИЕ"
            daily_rows.append(
                [
                    result.symbol,
                    _fmt_price(result.current_price),
                    label,
                    _fmt_price(level.price),
                    f"{level.distance_pct}%",
                    result.scan_time,
                ]
            )

    if daily_rows:
        daily_rows.sort(key=lambda r: float(r[4].rstrip("%")))

        print("  === ДНЕВНЫЕ УРОВНИ (вчерашняя свеча) ===")
        print()

        headers = ["Монета", "Цена", "Тип", "Уровень", "Расст. %", "Время"]
        print(tabulate(daily_rows, headers=headers, tablefmt="simple", stralign="right"))
        print()

    total_signals = len(poc_rows) + len(daily_rows)
    print(
        f"  Сигналов: {total_signals} (POC: {len(poc_rows)}, Дневные: {len(daily_rows)}) | "
        f"Пар: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 100)
