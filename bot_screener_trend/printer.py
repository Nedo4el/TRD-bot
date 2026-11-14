"""Красивый вывод результатов скринера тренда в консоль."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

from tabulate import tabulate

from bot_screener_trend.scanner import ScanResult


def clear_console() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def format_turnover(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _row(r: ScanResult) -> list[object]:
    return [
        r.symbol,
        f"${r.price:.4f}",
        f"{r.ema_fast:.4f}",
        f"{r.ema_slow:.4f}",
        f"{r.spread_pct:.2f}%",
        f"{r.adx_value:.1f}",
        f"+{r.di_plus:.1f}/-{r.di_minus:.1f}",
        r.score,
        f"${r.stop_loss:.4f}",
        f"${r.take_profit:.4f}",
        format_turnover(r.turnover_24h),
    ]


HEADERS = [
    "Символ",
    "Цена",
    "EMA fast",
    "EMA slow",
    "Зазор",
    "ADX",
    "DI +/-",
    "Score",
    "SL",
    "TP",
    "Оборот",
]


def print_results(
    results: list[ScanResult],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
    min_score: int = 0,
) -> None:
    filtered = [r for r in results if r.score >= min_score]
    clear_console()

    now = datetime.now(MSK).strftime("%H:%M:%S MSK")

    up = [r for r in filtered if r.trend == "UP"]
    down = [r for r in filtered if r.trend == "DOWN"]

    print()
    print(f"  TREND SCREENER (EMA + ADX) — {now}")
    print(f"  Символов: {filtered_symbols}/{total_symbols} | Сигналов: {len(filtered)} | Время: {scan_time:.1f}s")
    print()

    # === Восходящий тренд ===
    if up:
        up_sorted = sorted(up, key=lambda r: r.score, reverse=True)
        print(f"  ▲ ВОСХОДЯЩИЙ ТРЕНД (EMA fast > slow, цена > macro, ADX): {len(up)}")
        print("  " + "─" * 110)
        print(tabulate([_row(r) for r in up_sorted], headers=HEADERS,
                       tablefmt="simple", stralign="right"))
        print()

    # === Нисходящий тренд ===
    if down:
        down_sorted = sorted(down, key=lambda r: r.score, reverse=True)
        print(f"  ▼ НИСХОДЯЩИЙ ТРЕНД (EMA fast < slow, цена < macro, ADX): {len(down)}")
        print("  " + "─" * 110)
        print(tabulate([_row(r) for r in down_sorted], headers=HEADERS,
                       tablefmt="simple", stralign="right"))
        print()

    if not filtered:
        print("  Нет сигналов")
        print()


def save_report(
    results: list[ScanResult],
    total_symbols: int,
    filtered_symbols: int,
    scan_time: float,
    reports_dir: str,
) -> str:
    """Сохранить отчёт в файл."""
    now = datetime.now(MSK).strftime("%Y-%m-%d_%H-%M")
    path = f"{reports_dir}/trend_{now}.txt"

    up = [r for r in results if r.trend == "UP"]
    down = [r for r in results if r.trend == "DOWN"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"TREND SCREENER (EMA + ADX) — {now} MSK\n")
        f.write(f"Символов: {filtered_symbols}/{total_symbols} | Сигналов: {len(results)} | Время: {scan_time:.1f}s\n\n")

        for title, group, arrow in (
            ("ВОСХОДЯЩИЙ ТРЕНД", up, "▲"),
            ("НИСХОДЯЩИЙ ТРЕНД", down, "▼"),
        ):
            if not group:
                continue
            f.write(f"{arrow} {title}: {len(group)}\n")
            f.write("─" * 60 + "\n")
            f.writelines(
                f"  {r.symbol:<12} ${r.price:<12.4f}  "
                f"EMA {r.ema_fast:.4f}/{r.ema_slow:.4f}  "
                f"ADX={r.adx_value:.1f}  Score={r.score}  "
                f"SL=${r.stop_loss:.4f}  TP=${r.take_profit:.4f}\n"
                for r in sorted(group, key=lambda x: x.score, reverse=True)
            )
            f.write("\n")

        if not results:
            f.write("Нет сигналов\n")

    return path
