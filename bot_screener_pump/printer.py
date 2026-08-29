"""Красивый вывод результатов Accumulation скринера."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from tabulate import tabulate

from bot_screener_pump.scanner import AccumulationSignal


def clear_console() -> None:
    """Очистить консоль."""
    os.system("cls" if os.name == "nt" else "clear")


def _fmt_volume(value: float) -> str:
    """Форматировать объем."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _factor_icon(value: float, threshold: float = 0.3) -> str:
    """Иконка фактора:更强 > threshold."""
    return "✅" if value >= threshold else "❌"


def _status_icon(status: str) -> str:
    """Иконка статуса."""
    icons = {
        "КРИТИЧЕСКИЙ": "🔥",
        "СИЛЬНЫЙ": "⚡",
        "УМЕРЕННЫЙ": "📊",
        "ШУМ": "💤",
    }
    return icons.get(status, "❓")


def print_results(
    results: list[AccumulationSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
    min_probability: float = 0.45,
) -> None:
    """Вывести таблицу результатов accumulation скринера.

    Args:
        results: результаты сканирования.
        total_symbols: общее количество символов.
        filtered_symbols: количество символов после фильтрации.
        scan_time: время сканирования в секундах.
        min_probability: мин. вероятность для отображения.
    """
    clear_console()

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    print("=" * 140)
    print("  ACCUMULATION SCREENER — Поиск накопления крупных игроков")
    print(f"  Bybit USDT-M | {now}")
    print("=" * 140)
    print()

    # Фильтруем по мин. вероятности
    filtered = [r for r in results if r.pump_probability >= min_probability]
    filtered.sort(key=lambda r: r.pump_probability, reverse=True)

    if not filtered:
        print("  Сигналов не найдено.")
        print()
        print(f"  Пар под фильтром: {filtered_symbols}/{total_symbols}")
        print(f"  Время сканирования: {scan_time:.1f} сек")
        print("=" * 140)
        return

    # Собираем таблицу
    rows = []
    for rank, r in enumerate(filtered, 1):
        rows.append(
            [
                rank,
                r.symbol,
                r.timeframe,
                f"{r.price:.4f}" if r.price < 10 else f"{r.price:.2f}",
                f"{r.range_pct}%",
                _fmt_volume(r.avg_volume),
                f"{r.buy_sell_ratio:.2f}",
                f"{_factor_icon(r.f_range)} {r.f_range:.2f}",
                f"{_factor_icon(r.f_volume, 0.3)} {r.f_volume:.2f}",
                f"{_factor_icon(r.f_obv, 0.3)} {r.f_obv:.2f}",
                f"{_factor_icon(r.f_bb, 0.3)} {r.f_bb:.2f}",
                f"{_factor_icon(r.f_smart_money, 0.3)} {r.f_smart_money:.2f}",
                f"{r.pump_probability:.1%}",
                f"{_status_icon(r.status)} {r.status}",
            ]
        )

    headers = [
        "#",
        "Монета",
        "TF",
        "Цена",
        "Диапазон",
        "Объем (ср.)",
        "B/S",
        "Диапазон",
        "Объем",
        "OBV",
        "BB",
        "Smart $",
        "Вероятн.",
        "Статус",
    ]

    print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))

    # Детальная таблица по топ-3
    top3 = filtered[:3]
    if top3:
        print()
        print("  ДЕТАЛИЗАЦИЯ ТОП-3:")
        print("  " + "-" * 100)

        for r in top3:
            print(
                f"  {r.symbol} [{r.timeframe}] "
                f"P={r.pump_probability:.1%} "
                f"Диапазон={r.range_pct}% (поз={r.range_position:.0%}) "
                f"B/S={r.buy_sell_ratio:.2f}"
            )
            print(
                f"    F1(Диапазон)={r.f_range:.2f}  "
                f"F2(Объем)={r.f_volume:.2f}  "
                f"F3(OBV)={r.f_obv:.2f}  "
                f"F4(BB)={r.f_bb:.2f}"
            )
            print(
                f"    F5(Smart)={r.f_smart_money:.2f}  "
                f"F6(Отток)={r.f_outflow:.2f}  "
                f"F7(RSI)={r.f_rsi:.2f}  "
                f"F8(Ликв.)={r.f_liquidity:.2f}"
            )
            print()

    print(
        f"  Сигналов: {len(filtered)} | "
        f"Пар под фильтром: {filtered_symbols}/{total_symbols} | "
        f"Время: {scan_time:.1f} сек"
    )
    print("=" * 140)


def print_backtest(results: dict) -> None:
    """Вывести результаты бэктеста."""
    print()
    print("=" * 80)
    print("  BACKTEST RESULTS")
    print("=" * 80)

    if "error" in results:
        print(f"  Ошибка: {results['error']}")
        print("=" * 80)
        return

    print(f"  Символ: {results.get('symbol', 'N/A')}")
    print(f"  Всего сигналов: {results['total_signals']}")
    print()

    hold_days = results.get("hold_days", [])
    precision = results.get("precision", {})
    avg_return = results.get("avg_return", {})

    for days in hold_days:
        p = precision.get(days, "0.0%")
        r = avg_return.get(days, "+0.0%")
        print(f"  Через {days:>2} дн: точность={p:>6}  ср. прибыль={r:>8}")

    print("=" * 80)
