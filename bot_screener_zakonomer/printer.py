"""Вывод результатов анализа спайков (консоль + markdown-отчёт)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tabulate import tabulate

from bot_screener_zakonomer.scanner import SpikeConfig, SymbolStats

MSK = timezone(timedelta(hours=3))
WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt_dt(ts_ms: int) -> str:
    """Время спайка в МСК: YYYY-MM-DD HH:MM:SS."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=MSK).strftime("%Y-%m-%d %H:%M:%S")


def render(
    results: list[SymbolStats],
    *,
    total_symbols: int,
    filtered_symbols: int,
    no_spikes: int,
    scan_time: float,
    cfg: SpikeConfig,
) -> str:
    """Собрать markdown-отчёт одной строкой."""
    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []

    lines.append(
        f"# SPIKE SCAN: прострел {cfg.spike_pct:.0%} за {cfg.window_sec} секунд",
    )
    lines.append("")
    lines.append(f"- дата: {now} MSK")
    lines.append(f"- период: {cfg.lookback_days} дней, timeframe={cfg.interval}")
    lines.append(
        f"- оборот 24ч: >= ${cfg.min_turnover_24h / 1_000_000:.0f}M, "
        f"исключено топ: {len(cfg.exclude_symbols)}",
    )
    lines.append(f"- cooldown между спайками: {cfg.cooldown_sec}с")
    lines.append("- источник: Bybit kline M1, synthetic 1S (не publicTrade)")
    lines.append(
        f"- пар в обороте: {filtered_symbols}/{total_symbols}, "
        f"без спайков: {no_spikes}, время: {scan_time:.1f}с",
    )
    lines.append("")

    if not results:
        lines.append("Спайков не найдено.")
        return "\n".join(lines) + "\n"

    rows = []
    for s in results:
        top_hour = s.hour_counts.most_common(1)
        th = f"{top_hour[0][0]:02d}:00 ({top_hour[0][1]})" if top_hour else "-"
        rows.append(
            [
                s.symbol,
                s.longs,
                s.shorts,
                s.total,
                f"{s.total / cfg.lookback_days:.1f}",
                f"{s.max_bar_range_pct:.1%}",
                th,
            ],
        )
    lines.append(
        tabulate(
            rows,
            headers=[
                "symbol",
                "LONG",
                "SHORT",
                "total",
                "spikes/day",
                "max M1 range",
                "top hour MSK",
            ],
            tablefmt="github",
        ),
    )

    for s in results:
        lines.append("")
        lines.append(f"## {s.symbol} (LONG {s.longs} / SHORT {s.shorts})")

        hours = s.hour_counts.most_common(5)
        if hours:
            lines.append(
                "часы MSK: " + "  ".join(f"{h:02d}:00({c})" for h, c in hours),
            )
        days = s.weekday_counts.most_common()
        if days:
            wd = "  ".join(f"{WEEKDAYS_RU[d]}({c})" for d, c in days)
            lines.append(f"дни недели: {wd}")

        top = sorted(s.spikes, key=lambda x: abs(x.move_pct), reverse=True)[:5]
        if top:
            lines.append("топ спайков:")
            for sp in top:
                lines.append(
                    f"- {_fmt_dt(sp.ts_ms)} MSK  {sp.direction}  "
                    f"{sp.move_pct:+.2%}  price={sp.price}",
                )

    return "\n".join(lines) + "\n"


def print_results(
    results: list[SymbolStats],
    *,
    total_symbols: int,
    filtered_symbols: int,
    no_spikes: int,
    scan_time: float,
    cfg: SpikeConfig,
    report_file: str,
) -> None:
    """Вывести отчёт в консоль и сохранить в файл."""
    text = render(
        results,
        total_symbols=total_symbols,
        filtered_symbols=filtered_symbols,
        no_spikes=no_spikes,
        scan_time=scan_time,
        cfg=cfg,
    )
    print(text)

    path = Path(report_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"Отчёт сохранён: {path}")
