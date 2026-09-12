"""Вывод результатов скринера круглых чисел."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot_screener_krugloe.scanner import RoundSignal

MSK = timezone(timedelta(hours=3))


def print_results(
    results: list[RoundSignal],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
    proximity_pct: float,
) -> None:
    """Вывести таблицу результатов."""
    now = datetime.now(MSK).strftime("%H:%M:%S MSK")
    header = (
        f"\n{'='*130}\n"
        f"  СКРИНЕР КРУГЛЫХ ЧИСЕЛ — Психологические уровни\n"
        f"  Bybit USDT-M | {now} | Проксимити: {proximity_pct}%\n"
        f"{'='*130}"
    )
    print(header)

    if not results:
        print(
            f"\n  Сигналов не найдено.\n\n"
            f"  Пар под фильтром: {filtered_symbols}/{total_symbols}\n"
            f"  Время сканирования: {scan_time:.1f} сек\n"
        )
        print("=" * 130)
        return

    # Заголовок таблицы
    print(
        f"\n  {'#':>3}  {'Монета':<14} {'TF':>4} {'Цена':>10} "
        f"{'Уровень':>10} {'Расст.%':>8} {'До верх':>10} {'До низ':>10} {'Оборот':>10} {'Время':>8}"
    )
    print("  " + "-" * 134)

    for i, r in enumerate(results, 1):
        print(
            f"  {i:>3}  {r.symbol:<14} {r.timeframe:>4} {r.price:>10.4f} "
            f"{r.level:>10.4f} {r.proximity_pct:>7.2f}% "
            f"{r.dist_up_pct:>9.2f}% {r.dist_down_pct:>9.2f}% "
            f"${r.turnover_24h/1e6:>7.1f}M "
            f"{r.signal_time:>8}"
        )

    print(f"\n  Сигналов: {len(results)} | Пар под фильтром: {filtered_symbols}/{total_symbols} | Время: {scan_time:.1f} сек")
    print("=" * 130)
