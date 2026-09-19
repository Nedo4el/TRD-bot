"""Сохранить итоговый отчёт 30-дневного бэктеста."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

MSK = timezone(timedelta(hours=3))
REPORT_DIR = Path(r"C:\Users\79095\Desktop\trd\Отчеты скринера")

now_str = datetime.now(tz=MSK).strftime("%Y%m%d_%H%M")
date_str = datetime.now(tz=MSK).strftime("%d.%m.%Y %H:%M MSK")

results = [
    ("AKEUSDT", 45, 28, 17, 40.0, 23.06, 1.51),
    ("BTRUSDT", 57, 20, 37, 68.0, 69.98, 2.54),
    ("BRUSDT", 56, 29, 27, 48.0, 49.13, 2.00),
    ("LSKUSDT", 88, 33, 55, 39.0, 29.55, 1.33),
    ("NILUSDT", 24, 16, 8, 46.0, 3.86, 1.15),
]

total_trades = sum(r[1] for r in results)
avg_pnl = sum(r[5] for r in results) / len(results)

SEP = "=" * 70
DASH = "-" * 70

lines = [
    SEP,
    "  ИТОГИ 30-ДНЕВНОГО БЭКТЕСТА | robot_flat | M5",
    SEP,
    "",
    f"  ДАТА: {date_str}",
    "",
    "  ПАРАМЕТРЫ СТРАТЕГИИ:",
    "    POC:               Volume Profile (100 бинов, объём по high/low)",
    "    POC окно:          600 свечей M5 (50 часов), фиксируется при старте",
    "    Коридор:           ±10% от POC (RANGE_PCT=20)",
    "    Стоп зона:         ±5% от POC (запрет ордеров)",
    "    Сетка LONG:        -6%, -8%, -10% от POC",
    "    Сетка SHORT:       +6%, +8%, +10% от POC",
    "    SL:                ±13% от POC (граница коридора + 3%)",
    "    TP:                противоположный ордер -1% ближе к POC",
    "    Трейлинг:          2% от пика после POC",
    "    Частичное закрытие: 50% на POC",
    "    Макс позиций:      3 в одну сторону",
    "    Размер ордера:     $1",
    "",
    "  ПАРАМЕТРЫ БЭКТЕСТА:",
    "    Период:            30 дней (M5 = 8640 свечей)",
    "    Bybit:             mainnet (TESTNET=false)",
    "    Инструменты:       AKEUSDT, BTRUSDT, BRUSDT, LSKUSDT, NILUSDT",
    "",
    DASH,
    "  РЕЗУЛЬТАТЫ ПО МОНЕТАМ:",
    DASH,
    "",
]

header = "  {:<10s} {:>6s} {:>5s} {:>6s} {:>6s} {:>8s} {:>5s}".format(
    "Монета", "Сделок", "LONG", "SHORT", "Win%", "PnL", "PF"
)
lines.append(header)
lines.append("  " + "-" * 52)

for sym, trades, longs, shorts, win, pnl, pf in results:
    lines.append(
        "  {:<10s} {:6d} {:5d} {:6d} {:5.1f}% {:+7.2f}% {:5.2f}".format(
            sym, trades, longs, shorts, win, pnl, pf
        )
    )

lines.append("  " + "-" * 52)
lines.append(
    "  {:<10s} {:6d} {:>5s} {:>6s} {:>6s} {:+7.2f}% (ср.)".format(
        "ИТОГО", total_trades, "", "", "", avg_pnl
    )
)

lines += [
    "",
    DASH,
    "  ВЫВОДЫ:",
    DASH,
    "",
    "  1. Все 5 монет прибыльны на 30-дневном периоде.",
    "  2. Лучший результат: BTRUSDT (+70%, PF 2.54, 68% win)",
    "  3. Самый стабильный: BRUSDT (+49%, PF 2.00)",
    "  4. Наименьшая просадка: LSKUSDT (39% win, но +30% PnL)",
    "  5. Единственный с малым PnL: NILUSDT (+4%)",
    "  6. SHORT работает: до 37 шортовых сделок на BTRUSDT",
    "  7. Средний win rate: ~48%, средний PnL: +35%",
    "",
    SEP,
]

filepath = REPORT_DIR / f"backtest_30d_SUMMARY_{now_str}.txt"
filepath.write_text("\n".join(lines), encoding="utf-8")
print(f"Saved: {filepath}")
print(f"Size: {filepath.stat().st_size} bytes")
