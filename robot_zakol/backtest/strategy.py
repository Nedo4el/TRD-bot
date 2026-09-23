"""Стратегия: place/cancel TTL-лимитки + управление позицией SL/BE/trail."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from robot_zakol.backtest.config import StrategyConfig
from robot_zakol.backtest.fill_model import FillModel, PendingLimit

logger = logging.getLogger(__name__)


@dataclass
class TradeLog:
    """Одна завершённая сделка (все поля для лога по ТЗ)."""

    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    reason: str
    pnl: float
    pnl_pct: float
    slippage: float
    was_partial: bool
    qty: float
    hold_ms: int = 0

    @property
    def is_win(self) -> bool:
        return self.pnl > 0


@dataclass
class Counters:
    """Счётчики fill-механики."""

    post_only_rejects: int = 0
    partial_fills: int = 0
    queue_rejects: int = 0
    ttl_cancels: int = 0
    re_places: int = 0
    extends: int = 0
    false_fills: int = 0
    entry_slippage_sum: float = 0.0
    exit_slippage_sum: float = 0.0
    fills: int = 0


@dataclass
class Position:
    """Открытая лонг-позиция."""

    entry_price: float
    qty: float
    peak: float
    stop: float
    be_active: bool = False
    trail_stop: float = 0.0
    opened_ts: int = 0
    was_partial: bool = False
    fill_price: float = 0.0


def limit_target(price: float, offset_pct: float, tick_size: float) -> float:
    """BUY limit = current * (1 - offset), tick-align."""
    raw = price * (1.0 - offset_pct)
    if tick_size <= 0:
        return raw
    return round(raw / tick_size) * tick_size


class StrategyState:
    """State machine IDLE | WORKING | IN_POSITION (+ kill)."""

    def __init__(
        self,
        cfg: StrategyConfig,
        fill: FillModel,
        deposit: float,
    ) -> None:
        self.cfg = cfg
        self.fill_model = fill
        self.deposit = deposit
        self.phase = "IDLE"
        self.pending: PendingLimit | None = None
        self.position: Position | None = None
        self.session_pnl = 0.0
        self.consecutive_losses = 0
        self.killed = False
        self.counters = Counters()
        self.trades: list[TradeLog] = []
        self.ref_price: float | None = None

    @property
    def notional(self) -> float:
        return self.deposit * (self.cfg.position_pct / 100.0)

    def qty_for(self, price: float) -> float:
        if price <= 0:
            return 0.0
        return self.notional / price


def should_kill(st: StrategyState, cfg: StrategyConfig) -> bool:
    if st.session_pnl <= -abs(cfg.max_loss_usd):
        return True
    return st.consecutive_losses >= cfg.max_consecutive_losses


def update_position_risk(
    pos: Position,
    price: float,
    cfg: StrategyConfig,
) -> tuple[bool, str | None]:
    """Обновить peak/BE/trail. Возвращает (need_server_update, exit_reason|None)."""
    pos.peak = max(pos.peak, price)
    if not pos.be_active and price >= pos.entry_price * (1.0 + cfg.be_trigger_pct):
        pos.be_active = True
    trail = pos.peak * (1.0 - cfg.trail_pct)
    pos.trail_stop = trail
    base = pos.entry_price if pos.be_active else pos.entry_price * (1.0 - cfg.stop_pct)
    new_stop = max(pos.stop, base, trail)
    need = abs(new_stop - pos.stop) > 0
    pos.stop = new_stop
    if price <= pos.stop:
        if pos.be_active and abs(pos.stop - pos.entry_price) < 1e-12:
            return need, "be"
        if pos.stop > pos.entry_price * (1.0 - cfg.stop_pct) + 1e-12:
            if abs(pos.stop - trail) < 1e-12 and pos.be_active:
                return need, "trail"
            if pos.stop > pos.entry_price:
                return need, "trail"
            return need, "sl"
        return need, "sl"
    return need, None
