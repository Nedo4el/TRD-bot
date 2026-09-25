"""Метрики бэктеста: WR, PF, DD, Sharpe, recovery, распределение выходов."""

from __future__ import annotations

import math
from dataclasses import dataclass

from robot_zakol.backtest.data_loader import Series
from robot_zakol.backtest.strategy import StrategyState, TradeLog


@dataclass
class Metrics:
    """Сводные метрики одного прогона."""

    trades: int
    wins: int
    win_rate: float
    profit_factor: float
    max_dd_pct: float
    max_dd_usd: float
    avg_pnl_usd: float
    avg_pnl_pct: float
    avg_hold_ms: float
    sum_pnl_usd: float
    final_equity: float
    pnl_pct_of_deposit: float
    exit_sl: float
    exit_trail: float
    exit_be: float
    exit_tp: float
    exit_end: float
    exit_sl_pct: float
    exit_trail_pct: float
    exit_be_pct: float
    exit_tp_pct: float
    exit_end_pct: float
    false_fill_pct: float
    partial_fill_pct: float
    post_only_reject_pct: float
    avg_entry_slippage: float
    avg_exit_slippage: float
    sharpe: float
    recovery_factor: float
    kills: int
    ttl_cancels: int
    re_places: int
    queue_rejects: int
    post_only_rejects: int


def build_metrics(st: StrategyState, deposit: float, series: Series) -> Metrics:
    """Собрать Metrics из состояния и сделок."""
    trades = st.trades
    n = len(trades)
    wins = sum(1 for t in trades if t.is_win)
    losses = [t for t in trades if t.pnl <= 0]
    wins_pnl = [t.pnl for t in trades if t.pnl > 0]
    loss_pnl = [abs(t.pnl) for t in losses]
    gross_win = sum(wins_pnl)
    gross_loss = sum(loss_pnl)
    pf = (
        gross_win / gross_loss
        if gross_loss > 0
        else (math.inf if gross_win > 0 else 0.0)
    )
    if math.isinf(pf):
        pf = 999.99

    eq = deposit
    peak = deposit
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    rets: list[float] = []
    for t in trades:
        eq += t.pnl
        rets.append(t.pnl / peak if peak > 0 else 0.0)
        peak = max(peak, eq)
        dd = peak - eq
        max_dd_usd = max(max_dd_usd, dd)
        max_dd_pct = max(max_dd_pct, (dd / peak * 100.0) if peak > 0 else 0.0)

    mean_r = sum(rets) / len(rets) if rets else 0.0
    var = sum((r - mean_r) ** 2 for r in rets) / len(rets) if rets else 0.0
    std = math.sqrt(var)
    sharpe = (mean_r / std * math.sqrt(len(rets))) if std > 1e-12 else 0.0
    recovery = (eq - deposit) / max_dd_usd if max_dd_usd > 0 else 0.0

    reasons = {"sl": 0, "trail": 0, "be": 0, "tp": 0, "end": 0, "max_loss": 0}
    for t in trades:
        key = t.reason if t.reason in reasons else "end"
        reasons[key] = reasons.get(key, 0) + 1

    fills = max(st.counters.fills, 1)
    partials = sum(1 for t in trades if t.was_partial)
    avg_hold = sum(t.hold_ms for t in trades) / n if n else 0.0
    avg_pnl = sum(t.pnl for t in trades) / n if n else 0.0
    avg_pnl_pct = sum(t.pnl_pct for t in trades) / n if n else 0.0
    avg_entry_slip = st.counters.entry_slippage_sum / fills
    avg_exit_slip = st.counters.exit_slippage_sum / n if n else 0.0

    total = max(n, 1)
    return Metrics(
        trades=n,
        wins=wins,
        win_rate=wins / n if n else 0.0,
        profit_factor=pf,
        max_dd_pct=max_dd_pct,
        max_dd_usd=max_dd_usd,
        avg_pnl_usd=avg_pnl,
        avg_pnl_pct=avg_pnl_pct,
        avg_hold_ms=avg_hold,
        sum_pnl_usd=sum(t.pnl for t in trades),
        final_equity=eq,
        pnl_pct_of_deposit=((eq - deposit) / deposit * 100.0) if deposit else 0.0,
        exit_sl=reasons["sl"],
        exit_trail=reasons["trail"],
        exit_be=reasons["be"],
        exit_tp=reasons["tp"],
        exit_end=reasons["end"],
        exit_sl_pct=reasons["sl"] / total,
        exit_trail_pct=reasons["trail"] / total,
        exit_be_pct=reasons["be"] / total,
        exit_tp_pct=reasons["tp"] / total,
        exit_end_pct=reasons["end"] / total,
        false_fill_pct=st.counters.false_fills / total,
        partial_fill_pct=partials / total,
        post_only_reject_pct=st.counters.post_only_rejects
        / max(st.counters.post_only_rejects + fills, 1),
        avg_entry_slippage=avg_entry_slip,
        avg_exit_slippage=avg_exit_slip,
        sharpe=sharpe,
        recovery_factor=recovery,
        kills=1 if st.killed else 0,
        ttl_cancels=st.counters.ttl_cancels,
        re_places=st.counters.re_places,
        queue_rejects=st.counters.queue_rejects,
        post_only_rejects=st.counters.post_only_rejects,
    )


def equity_curve(deposit: float, trades: list[TradeLog]) -> list[float]:
    """Кривая equity после каждой сделки (включая начальный депозит)."""
    eq = [deposit]
    acc = deposit
    for t in trades:
        acc += t.pnl
        eq.append(acc)
    return eq
