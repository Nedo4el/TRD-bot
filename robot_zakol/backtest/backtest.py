"""Движок бэктеста: event-loop по тикам, бракет-сетка TTL-лимиток, fill, exit."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from robot_zakol.backtest.config import FillConfig, StrategyConfig, mode_presets
from robot_zakol.backtest.data_loader import Series, Tick
from robot_zakol.backtest.fill_model import FillModel, PendingLimit
from robot_zakol.backtest.metrics import Metrics, build_metrics
from robot_zakol.backtest.strategy import (
    Position,
    StrategyState,
    TradeLog,
    limit_target,
    should_kill,
    update_position_risk,
)

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Итог одного прогона."""

    mode: str
    metrics: Metrics
    trades: list[TradeLog]
    fidelity: str
    source_note: str
    warnings: list[str]


def run_single(
    series: Series,
    strat: StrategyConfig,
    fill_cfg: FillConfig,
    mode_name: str,
) -> BacktestResult:
    """Прогнать один режим fill-модели по тик-ряду."""
    fill_model = FillModel(fill_cfg, strat.tick_size)
    st = StrategyState(strat, fill_model, strat.deposit_usd)
    prev_price: float | None = None

    for tick in series.ticks:
        if st.killed:
            break
        price = tick.price

        if st.phase == "IN_POSITION" and st.position is not None:
            _step_position(st, tick, strat, fill_cfg, prev_price)
            if st.phase == "IDLE" and should_kill(st, strat):
                st.killed = True
                break
            prev_price = price
            continue

        if st.phase == "WORKING" and st.pendings():
            _step_working(st, tick, strat, fill_cfg, prev_price)
            prev_price = price
            continue

        if st.phase == "IDLE" and not st.killed:
            _place(st, tick, strat)
        prev_price = price

    if st.position is not None:
        last = series.ticks[-1] if series.ticks else None
        if last is not None:
            _force_close(st, last, strat, fill_cfg, "end")

    warnings = _warnings(st)
    metrics = build_metrics(st, strat.deposit_usd, series)
    return BacktestResult(
        mode=mode_name,
        metrics=metrics,
        trades=st.trades,
        fidelity=series.fidelity,
        source_note=series.source_note,
        warnings=warnings,
    )


def _place(st: StrategyState, tick: Tick, strat: StrategyConfig) -> None:
    """Поставить бракет-сетку: лонги −grid, шорты +grid (если не long_only)."""
    grid: tuple[float, ...] = strat.grid_pcts
    if not grid:
        if strat.offset_pct <= 0:
            return
        grid = (strat.offset_pct,)
    qty_total = st.qty_for(tick.price)
    if qty_total <= 0:
        return
    qty_each = qty_total / len(grid)
    deadline = tick.ts_ms + int(strat.ttl_sec * 1000)
    st.pending_buys = [
        PendingLimit(
            target=limit_target(tick.price, pct, strat.tick_size, "Buy"),
            qty=qty_each,
            placed_ts_ms=tick.ts_ms,
            deadline_ts_ms=deadline,
            side="Buy",
        )
        for pct in grid
    ]
    st.pending_sells = []
    if not strat.long_only:
        st.pending_sells = [
            PendingLimit(
                target=limit_target(tick.price, pct, strat.tick_size, "Sell"),
                qty=qty_each,
                placed_ts_ms=tick.ts_ms,
                deadline_ts_ms=deadline,
                side="Sell",
            )
            for pct in grid
        ]
    st.ref_price = tick.price
    st.phase = "WORKING"


def _step_working(
    st: StrategyState,
    tick: Tick,
    strat: StrategyConfig,
    fill_cfg: FillConfig,
    prev_price: float | None,
) -> None:
    ready = _collect_fills(st, tick, strat, fill_cfg, prev_price)
    if ready:
        # филл ордера или группы: открываем позицию, остальное снимаем
        _open_position(st, ready, tick, strat)
        return
    orders = st.pendings()
    if not orders or not any(o.active for o in orders):
        _drop_grid(st)
        st.phase = "IDLE"
        return
    if tick.ts_ms < orders[0].deadline_ts_ms:
        return
    st.counters.ttl_cancels += 1
    ref = st.ref_price or tick.price
    delta = abs(tick.price - ref) / ref if ref else 1.0
    if delta < strat.min_price_change:
        st.counters.extends += 1
    else:
        st.counters.re_places += 1
    _drop_grid(st)
    st.phase = "IDLE"
    _place(st, tick, strat)


def _collect_fills(
    st: StrategyState,
    tick: Tick,
    strat: StrategyConfig,
    fill_cfg: FillConfig,
    prev_price: float | None,
) -> list[PendingLimit]:
    """Прогнать тик по всей сетке, вернуть ордера, готовые к открытию."""
    for order in st.pendings():
        if not order.active:
            continue
        outcome = st.fill_model.on_tick(
            order,
            tick.price,
            tick.size,
            prev_price,
            tick.ts_ms,
        )
        if outcome.post_only_reject:
            st.counters.post_only_rejects += 1
            order.active = False
            continue
        if outcome.queue_reject:
            st.counters.queue_rejects += 1
        if outcome.filled_qty > 0:
            order.filled_qty += outcome.filled_qty
            st.counters.fills += 1
            if outcome.partial:
                st.counters.partial_fills += 1
            if order.side == "Sell":
                slip = max(0.0, outcome.fill_price - order.target)
            else:
                slip = max(0.0, order.target - outcome.fill_price)
            st.counters.entry_slippage_sum += slip
    ready: list[PendingLimit] = []
    for order in st.pendings():
        if order.filled_qty <= 0:
            continue
        ratio = order.filled_qty / order.qty if order.qty else 0.0
        if (
            ratio >= strat.partial_fill_pct
            or fill_cfg.ideal
            or tick.ts_ms >= order.deadline_ts_ms
        ):
            ready.append(order)
    return ready


def _drop_grid(st: StrategyState) -> None:
    st.pending_buys.clear()
    st.pending_sells.clear()


def _open_position(
    st: StrategyState,
    ready: list[PendingLimit],
    tick: Tick,
    strat: StrategyConfig,
) -> None:
    """Открыть позицию из группы филлнувшихся ордеров одной стороны."""
    qty = sum(o.filled_qty for o in ready)
    if qty <= 0:
        return
    entry = sum(o.target * o.filled_qty for o in ready) / qty
    long_qty = sum(o.filled_qty for o in ready if o.side != "Sell")
    side = "long" if long_qty * 2 >= qty else "short"
    if 0.0 < long_qty < qty:
        logger.warning(
            "групповой вход с обеих сторон: long=%.6g short=%.6g",
            long_qty,
            qty - long_qty,
        )
    was_partial = any(o.filled_qty < o.qty - 1e-12 for o in ready)
    if strat.stop_delay_sec <= 0:
        stop = (
            entry * (1.0 - strat.stop_pct)
            if side == "long"
            else entry * (1.0 + strat.stop_pct)
        )
        armed = True
    else:
        stop = entry
        armed = False
    pos = Position(
        entry_price=entry,
        qty=qty,
        peak=entry,
        stop=stop,
        side=side,
        opened_ts=tick.ts_ms,
        was_partial=was_partial,
        fill_price=entry,
        stop_armed=armed,
    )
    _drop_grid(st)
    st.position = pos
    st.phase = "IN_POSITION"


def _step_position(
    st: StrategyState,
    tick: Tick,
    strat: StrategyConfig,
    fill_cfg: FillConfig,
    prev_price: float | None,
) -> None:
    pos = st.position
    if pos is None:
        st.phase = "IDLE"
        return
    delay_ms = int(strat.stop_delay_sec * 1000)
    if not pos.stop_armed and tick.ts_ms >= pos.opened_ts + delay_ms:
        # стоп через stop_delay_sec: stop_pct от текущей цены
        if pos.side == "long":
            pos.stop = tick.price * (1.0 - strat.stop_pct)
        else:
            pos.stop = tick.price * (1.0 + strat.stop_pct)
        pos.stop_armed = True
    _need, exit_reason = update_position_risk(pos, tick.price, strat)
    if exit_reason is None:
        return
    _close(st, pos, tick, fill_cfg, exit_reason, strat, prev_price)


def _close(
    st: StrategyState,
    pos: Position,
    tick: Tick,
    fill_cfg: FillConfig,
    reason: str,
    strat: StrategyConfig,
    prev_price: float | None,
) -> None:
    if reason == "tp":
        raw = pos.entry_price * (
            (1.0 + strat.take_pct) if pos.side == "long" else (1.0 - strat.take_pct)
        )
        exit_price = raw
        slip = 0.0
    else:
        raw = pos.stop if reason in ("sl", "trail", "be") else tick.price
        side = "sell" if pos.side == "long" else "buy"
        exit_price = st.fill_model.exit_slippage(raw, side)
        slip = abs(exit_price - raw)
    st.counters.exit_slippage_sum += slip
    if pos.side == "short":
        pnl = (pos.entry_price - exit_price) * pos.qty
        pnl_pct = (
            (pos.entry_price - exit_price) / pos.entry_price * 100.0
            if pos.entry_price
            else 0.0
        )
    else:
        pnl = (exit_price - pos.entry_price) * pos.qty
        pnl_pct = (
            (exit_price / pos.entry_price - 1.0) * 100.0 if pos.entry_price else 0.0
        )
    st.session_pnl += pnl
    st.peak_session_pnl = max(st.peak_session_pnl, st.session_pnl)
    trade = TradeLog(
        entry_ts=pos.opened_ts,
        entry_price=pos.entry_price,
        exit_ts=tick.ts_ms,
        exit_price=exit_price,
        reason=reason,
        pnl=pnl,
        pnl_pct=pnl_pct,
        slippage=slip,
        was_partial=pos.was_partial,
        qty=pos.qty,
        hold_ms=tick.ts_ms - pos.opened_ts,
        side=pos.side,
    )
    st.trades.append(trade)
    st.position = None
    st.phase = "IDLE"


def _force_close(
    st: StrategyState,
    tick: Tick,
    strat: StrategyConfig,
    fill_cfg: FillConfig,
    reason: str,
) -> None:
    pos = st.position
    if pos is None:
        return
    _close(st, pos, tick, fill_cfg, reason, strat, None)


def _warnings(st: StrategyState) -> list[str]:
    warns: list[str] = []
    trades = st.trades
    if not trades:
        warns.append("Нет сделок — fill-модель или данные не дали входов.")
        return warns
    trail_n = sum(1 for t in trades if t.reason == "trail")
    tp_n = sum(1 for t in trades if t.reason == "tp")
    sl_n = sum(1 for t in trades if t.reason == "sl")
    wins = sum(1 for t in trades if t.is_win)
    trail_pct = trail_n / len(trades)
    tp_pct = tp_n / len(trades)
    win_pct = wins / len(trades)
    if trail_pct > 0.80:
        warns.append(
            "Trail доминирует. TP и SL могут быть мёртвыми параметрами. "
            "Проверь, достижим ли TP за среднее время удержания.",
        )
    if tp_pct > 0.80:
        warns.append(
            "TP доминирует. Проверь, что стоп реально работает, "
            "а тейк не даёт ложную картину.",
        )
    if sl_n == 0:
        warns.append(
            "SL не сработал ни разу. Либо стратегия реально хороша, либо "
            "fill-модель идеалистична, либо SL слишком далеко.",
        )
    if win_pct > 0.90:
        warns.append("Win rate подозрительно высок. Проверь fill-модель.")
    return warns


def run_modes(
    series: Series,
    deposit: float = 100.0,
    modes: tuple[str, ...] = ("ideal", "realistic", "pessimistic"),
    strat: StrategyConfig | None = None,
) -> dict[str, BacktestResult]:
    """Прогнать несколько fill-моделей на одном ряде."""
    presets = mode_presets(deposit, strat)
    out: dict[str, BacktestResult] = {}
    for name in modes:
        if name not in presets:
            raise ValueError(f"unknown mode: {name}")
        s, fill = presets[name]
        out[name] = run_single(series, s, fill, name)
        logger.info(
            "mode=%s trades=%d pnl=%.4f fidelity=%s",
            name,
            out[name].metrics.trades,
            out[name].metrics.sum_pnl_usd,
            series.fidelity,
        )
    return out
