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
    print(f"  TREND SCREENER — {now}")
    print(f"  Символов: {filtered_symbols}/{total_symbols} | Сигналов: {len(filtered)} | Время: {scan_time:.1f}s")
    print()

    # === Восходящий тренд ===
    if up:
        up_sorted = sorted(up, key=lambda r: r.score, reverse=True)
        print(f"  ▲ ВОСХОДЯЩИЙ ТРЕНД (HH+HL): {len(up)}")
        print("  " + "─" * 95)

        headers = ["Символ", "Цена", "HH", "HL", "EMA", "ADX", "RSI", "Score", "SL", "TP", "Оборот"]
        rows = []
        for r in up_sorted:
            rows.append([
                r.symbol,
                f"${r.price:.4f}",
                r.hh_count,
                r.hl_count,
                f"{'>' if r.ema_fast > r.ema_slow else '<'}",
                f"{r.adx_value:.1f}",
                f"{r.rsi_value:.0f}",
                r.score,
                f"${r.stop_loss:.4f}",
                f"${r.take_profit:.4f}",
                format_turnover(r.turnover_24h),
            ])
        print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))
        print()

    # === Нисходящий тренд ===
    if down:
        down_sorted = sorted(down, key=lambda r: r.score, reverse=True)
        print(f"  ▼ НИСХОДЯЩИЙ ТРЕНД (LH+LL): {len(down)}")
        print("  " + "─" * 95)

        headers = ["Символ", "Цена", "LH", "LL", "EMA", "ADX", "RSI", "Score", "SL", "TP", "Оборот"]
        rows = []
        for r in down_sorted:
            rows.append([
                r.symbol,
                f"${r.price:.4f}",
                r.lh_count,
                r.ll_count,
                f"{'<' if r.ema_fast < r.ema_slow else '>'}",
                f"{r.adx_value:.1f}",
                f"{r.rsi_value:.0f}",
                r.score,
                f"${r.stop_loss:.4f}",
                f"${r.take_profit:.4f}",
                format_turnover(r.turnover_24h),
            ])
        print(tabulate(rows, headers=headers, tablefmt="simple", stralign="right"))
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
        f.write(f"TREND SCREENER — {now} MSK\n")
        f.write(f"Символов: {filtered_symbols}/{total_symbols} | Сигналов: {len(results)} | Время: {scan_time:.1f}s\n\n")

        if up:
            f.write(f"▲ ВОСХОДЯЩИЙ ТРЕНД (HH+HL): {len(up)}\n")
            f.write("─" * 60 + "\n")
            for r in sorted(up, key=lambda x: x.score, reverse=True):
                f.write(f"  {r.symbol:<12} ${r.price:<12.4f}  HH={r.hh_count} HL={r.hl_count}  ADX={r.adx_value:.1f}  Score={r.score}  SL=${r.stop_loss:.4f}  TP=${r.take_profit:.4f}\n")
            f.write("\n")

        if down:
            f.write(f"▼ НИСХОДЯЩИЙ ТРЕНД (LH+LL): {len(down)}\n")
            f.write("─" * 60 + "\n")
            for r in sorted(down, key=lambda x: x.score, reverse=True):
                f.write(f"  {r.symbol:<12} ${r.price:<12.4f}  LH={r.lh_count} LL={r.ll_count}  ADX={r.adx_value:.1f}  Score={r.score}  SL=${r.stop_loss:.4f}  TP=${r.take_profit:.4f}\n")
            f.write("\n")

        if not results:
            f.write("Нет сигналов\n")

    return path
