"""Отчёт: текст + таблица сравнения 3 режимов + лог сделок."""

from __future__ import annotations

from pathlib import Path

from robot_zakol.backtest.backtest import BacktestResult


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _num(x: float, d: int = 2) -> str:
    if abs(x) >= 999.0:
        return f"{x:.{d}f}"
    return f"{x:.{d}f}"


def trade_lines(res: BacktestResult) -> list[str]:
    lines = []
    for i, t in enumerate(res.trades, 1):
        lines.append(
            f"  #{i:03d} entry_ts={t.entry_ts} entry={t.entry_price:.8g} "
            f"exit_ts={t.exit_ts} exit={t.exit_price:.8g} "
            f"reason={t.reason} pnl={t.pnl:.4f} ({t.pnl_pct:+.3f}%) "
            f"slip={t.slippage:.6f} partial={t.was_partial} "
            f"hold_ms={t.hold_ms}",
        )
    return lines


def comparison_table(results: dict[str, BacktestResult]) -> str:
    """Таблица сравнения режимов (markdown)."""
    header = (
        "| mode | trades | WR | PF | maxDD% | avg$ | sum$ | equity$ | "
        "Sharpe | RF | trail% | sl% | partial% | PO_rej% | fidelity |"
    )
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"
    rows = [header, sep]
    for name, res in results.items():
        m = res.metrics
        rows.append(
            f"| {name} | {m.trades} | {_pct(m.win_rate)} | {_num(m.profit_factor)} "
            f"| {_num(m.max_dd_pct)} | {_num(m.avg_pnl_usd, 3)} "
            f"| {_num(m.sum_pnl_usd, 3)} | {_num(m.final_equity)} "
            f"| {_num(m.sharpe)} | {_num(m.recovery_factor)} "
            f"| {_pct(m.exit_trail_pct)} | {_pct(m.exit_sl_pct)} "
            f"| {_pct(m.partial_fill_pct)} | {_pct(m.post_only_reject_pct)} "
            f"| {res.fidelity} |",
        )
    return "\n".join(rows)


def build_report(
    symbol: str,
    start: str,
    end: str,
    deposit: float,
    results: dict[str, BacktestResult],
    source_note: str = "",
) -> str:
    """Полный текстовый отчёт."""
    lines: list[str] = [
        "=== robot_zakol HONEST BACKTEST ===",
        f"symbol={symbol} dates={start} -> {end} deposit=${deposit:.0f}",
        f"source: {source_note}",
        "",
        "## Сравнение режимов",
        comparison_table(results),
        "",
    ]
    for name, res in results.items():
        m = res.metrics
        lines.append(f"## mode={name} fidelity={res.fidelity}")
        lines.append(f"source: {res.source_note}")
        lines.append(
            f"trades={m.trades} wins={m.wins} WR={_pct(m.win_rate)} "
            f"PF={_num(m.profit_factor)}",
        )
        lines.append(
            f"pnl={_num(m.sum_pnl_usd, 4)}$ ({_num(m.pnl_pct_of_deposit, 3)}% депо) "
            f"equity=${_num(m.final_equity)} kills={m.kills}",
        )
        lines.append(
            f"maxDD={_num(m.max_dd_pct, 3)}% / {_num(m.max_dd_usd, 4)}$ "
            f"Sharpe={_num(m.sharpe)} Recovery={_num(m.recovery_factor)}",
        )
        lines.append(
            f"avg_pnl={_num(m.avg_pnl_usd, 4)}$ avg%={_num(m.avg_pnl_pct, 3)} "
            f"avg_hold={m.avg_hold_ms / 1000:.1f}s",
        )
        lines.append(
            f"exits: trail={_pct(m.exit_trail_pct)} sl={_pct(m.exit_sl_pct)} "
            f"be={_pct(m.exit_be_pct)} end={_pct(m.exit_end_pct)}",
        )
        lines.append(
            f"fill: partial={_pct(m.partial_fill_pct)} "
            f"post_only_rej={_pct(m.post_only_reject_pct)} "
            f"false_fill={_pct(m.false_fill_pct)} "
            f"ttl_cancels={m.ttl_cancels} re_places={m.re_places} "
            f"queue_rej={m.queue_rejects} po_rej={m.post_only_rejects}",
        )
        lines.append(
            f"slippage: entry={m.avg_entry_slippage:.6f} "
            f"exit={m.avg_exit_slippage:.6f}",
        )
        for w in res.warnings:
            lines.append(f"WARNING: {w}")
        lines.append("")
        lines.append("### сделки")
        lines.extend(trade_lines(res))
        lines.append("")
    return "\n".join(lines)


def write_report(text: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text + "\n", encoding="utf-8")
    return p
