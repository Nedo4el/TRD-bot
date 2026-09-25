"""Стратегия: пара TTL-лимиток ±offset + управление позицией SL/TP/BE/trail."""

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
    side: str = "long"

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
    """Открытая позиция (long или short)."""

    entry_price: float
    qty: float
    peak: float
    stop: float
    side: str = "long"
    be_active: bool = False
    trail_stop: float = 0.0
    opened_ts: int = 0
    was_partial: bool = False
    fill_price: float = 0.0


def limit_target(
    price: float, offset_pct: float, tick_size: float, side: str = "Buy"
) -> float:
    """Цена лимитки: Buy = price*(1-offset), Sell = price*(1+offset)."""
    if side == "Sell":
        raw = price * (1.0 + offset_pct)
    else:
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
        self.pending_buy: PendingLimit | None = None
        self.pending_sell: PendingLimit | None = None
        self.position: Position | None = None
        self.session_pnl = 0.0
        self.peak_session_pnl = 0.0
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

    def pendings(self) -> list[PendingLimit]:
        return [p for p in (self.pending_buy, self.pending_sell) if p is not None]


def should_kill(st: StrategyState, cfg: StrategyConfig) -> bool:
    """Kill-switch. Значение 0 = лимит выключен."""
    if cfg.max_loss_usd > 0 and st.session_pnl <= -abs(cfg.max_loss_usd):
        return True
    if cfg.max_drawdown_pct <= 0:
        return False
    drawdown = st.peak_session_pnl - st.session_pnl
    return drawdown >= st.deposit * abs(cfg.max_drawdown_pct)


def take_hit(pos: Position, price: float, cfg: StrategyConfig) -> bool:
    """Цена достигла уровня тейка (take_pct <= 0 — тейк выключен)."""
    if cfg.take_pct <= 0:
        return False
    if pos.side == "short":
        return price <= pos.entry_price * (1.0 - cfg.take_pct)
    return price >= pos.entry_price * (1.0 + cfg.take_pct)


def update_position_risk(
    pos: Position,
    price: float,
    cfg: StrategyConfig,
) -> tuple[bool, str | None]:
    """Обновить peak/BE/trail. Возвращает (need_server_update, exit_reason|None).

    trail_pct <= 0 — трейлинг выключен, be_trigger_pct <= 0 — BE выключен.
    Если цена достигла тейка — exit_reason = "tp".
    """
    if take_hit(pos, price, cfg):
        return False, "tp"
    if pos.side == "short":
        return _update_short(pos, price, cfg)
    return _update_long(pos, price, cfg)


def _update_long(
    pos: Position, price: float, cfg: StrategyConfig
) -> tuple[bool, str | None]:
    pos.peak = max(pos.peak, price)
    if (
        not pos.be_active
        and cfg.be_trigger_pct > 0
        and price >= pos.entry_price * (1.0 + cfg.be_trigger_pct)
    ):
        pos.be_active = True
    base = (
        pos.entry_price * (1.0 + cfg.be_offset_pct)
        if pos.be_active
        else pos.entry_price * (1.0 - cfg.stop_pct)
    )
    trail = 0.0
    if cfg.trail_pct > 0:
        trail = pos.peak * (1.0 - cfg.trail_pct)
        pos.trail_stop = trail
        new_stop = max(pos.stop, base, trail)
    else:
        new_stop = max(pos.stop, base)
    need = abs(new_stop - pos.stop) > 0
    pos.stop = new_stop
    if price <= pos.stop:
        be_level = pos.entry_price * (1.0 + cfg.be_offset_pct)
        if pos.be_active and abs(pos.stop - be_level) < 1e-12:
            return need, "be"
        if pos.stop > pos.entry_price * (1.0 - cfg.stop_pct) + 1e-12:
            if cfg.trail_pct > 0 and abs(pos.stop - trail) < 1e-12 and pos.be_active:
                return need, "trail"
            if pos.stop > pos.entry_price:
                return need, "trail"
            return need, "sl"
        return need, "sl"
    return need, None


def _update_short(
    pos: Position, price: float, cfg: StrategyConfig
) -> tuple[bool, str | None]:
    """Зеркальный _update_long: минимум цены, стоп сверху."""
    pos.peak = min(pos.peak, price)
    if (
        not pos.be_active
        and cfg.be_trigger_pct > 0
        and price <= pos.entry_price * (1.0 - cfg.be_trigger_pct)
    ):
        pos.be_active = True
    base = (
        pos.entry_price * (1.0 - cfg.be_offset_pct)
        if pos.be_active
        else pos.entry_price * (1.0 + cfg.stop_pct)
    )
    trail = 0.0
    if cfg.trail_pct > 0:
        trail = pos.peak * (1.0 + cfg.trail_pct)
        pos.trail_stop = trail
        new_stop = min(pos.stop, base, trail)
    else:
        new_stop = min(pos.stop, base)
    need = abs(new_stop - pos.stop) > 0
    pos.stop = new_stop
    if price >= pos.stop:
        be_level = pos.entry_price * (1.0 - cfg.be_offset_pct)
        if pos.be_active and abs(pos.stop - be_level) < 1e-12:
            return need, "be"
        if pos.stop < pos.entry_price * (1.0 + cfg.stop_pct) - 1e-12:
            if cfg.trail_pct > 0 and abs(pos.stop - trail) < 1e-12 and pos.be_active:
                return need, "trail"
            if pos.stop < pos.entry_price:
                return need, "trail"
            return need, "sl"
        return need, "sl"
    return need, None
