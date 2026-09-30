"""Стратегия robot_trend: EMA + ADX/DI + Supertrend + ATR + Volume.

Роль индикаторов (параметры — в ``.env``, значения по умолчанию — в ``TrendParams``):

| # | Индикатор      | Параметры                       | Роль                          |
|---|----------------|---------------------------------|-------------------------------|
| 1 | EMA            | 20 / 50 / 200                   | направление тренда            |
| 2 | ADX (+DI/-DI)  | period=14, threshold=25         | сила тренда / фильтр флэта    |
| 3 | Supertrend     | ATR=10, factor=3.0 (альты);     | точка входа + трейлинг-стоп   |
|   |                | ATR=55, factor=2.0 (BTC/ETH)    |                               |
| 4 | ATR            | period=14                       | волатильность → размер стопа  |
| 5 | Volume SMA     | period=20, multiplier=1.3       | подтверждение пробоя          |

Вход: разворот Supertrend + тренд (EMA) + ADX >= порога + объём >= mult × SMA.
Выход: разворот Supertrend против позиции (серверный SL/TP — страховка).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.bybit_client import Candle
from core.indicators import adx_di, atr, ema, sma, supertrend
from core.strategies import BaseStrategy, Signal
from robot_trend.config import _env_float, _env_int
from robot_trend.state import OpenPosition


@dataclass(frozen=True)
class Instruction:
    """Решение стратегии. Исполняет технический слой (order_flow)."""

    action: str  # "enter" | "exit"
    side: str = ""  # "long" | "short" (для enter)
    stop: float | None = None  # уровень SL (для enter)
    take: float | None = None  # уровень TP (для enter)
    reason: str = ""


@dataclass(frozen=True)
class TrendParams:
    """Параметры индикаторов стратегии (из ``.env``, дефолты из таблицы)."""

    ema_fast: int = 20
    ema_slow: int = 50
    ema_macro: int = 200
    adx_period: int = 14
    adx_threshold: float = 25.0
    st_atr_period: int = 10
    st_factor: float = 3.0
    atr_period: int = 14
    sl_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    vol_period: int = 20
    vol_mult: float = 1.3

    @classmethod
    def from_env(cls, symbol: str | None = None) -> TrendParams:
        """Собрать параметры из окружения; профиль Supertrend по символу.

        BTC/ETH — длинный ATR (55) с factor 2.0, остальное — короткий (10/3.0).
        """
        sym = (symbol if symbol is not None else os.getenv("SYMBOL", "")).upper()
        major = sym.startswith(("BTC", "ETH"))
        return cls(
            ema_fast=_env_int("EMA_FAST", 20),
            ema_slow=_env_int("EMA_SLOW", 50),
            ema_macro=_env_int("EMA_MACRO", 200),
            adx_period=_env_int("ADX_PERIOD", 14),
            adx_threshold=_env_float("ADX_THRESHOLD", 25.0),
            st_atr_period=_env_int(
                "ST_ATR_PERIOD_MAJOR" if major else "ST_ATR_PERIOD",
                55 if major else 10,
            ),
            st_factor=_env_float(
                "ST_FACTOR_MAJOR" if major else "ST_FACTOR", 2.0 if major else 3.0
            ),
            atr_period=_env_int("ATR_PERIOD", 14),
            sl_atr_mult=_env_float("SL_ATR_MULT", 2.0),
            tp_atr_mult=_env_float("TP_ATR_MULT", 3.0),
            vol_period=_env_int("VOL_PERIOD", 20),
            vol_mult=_env_float("VOL_MULT", 1.3),
        )

    def __post_init__(self) -> None:
        periods = {
            "EMA_FAST": self.ema_fast,
            "EMA_SLOW": self.ema_slow,
            "EMA_MACRO": self.ema_macro,
            "ADX_PERIOD": self.adx_period,
            "ST_ATR_PERIOD": self.st_atr_period,
            "ATR_PERIOD": self.atr_period,
            "VOL_PERIOD": self.vol_period,
        }
        for name, value in periods.items():
            if value < 2:
                raise ValueError(f"{name} должен быть >= 2")
        if not self.ema_fast < self.ema_slow < self.ema_macro:
            raise ValueError("нужен порядок EMA_FAST < EMA_SLOW < EMA_MACRO")
        if self.adx_threshold < 0:
            raise ValueError("ADX_THRESHOLD не может быть < 0")
        if self.st_factor <= 0 or self.sl_atr_mult <= 0 or self.tp_atr_mult <= 0:
            raise ValueError("ST_FACTOR / SL_ATR_MULT / TP_ATR_MULT должны быть > 0")
        if self.vol_mult <= 0:
            raise ValueError("VOL_MULT должен быть > 0")


@dataclass(frozen=True)
class _Snapshot:
    """Значения индикаторов на последней закрытой свече."""

    close: float
    trend_up: bool  # fast > slow и close > macro
    trend_down: bool
    adx: float
    di_plus: float
    di_minus: float
    st_dir: int  # +1 / -1
    st_flip: int  # смена направления Supertrend на этой свече, иначе 0
    atr: float
    vol_ratio: float  # volume / SMA(volume)


class TrendStrategy(BaseStrategy):
    """Трендовая стратегия: EMA + ADX + Supertrend + ATR + Volume."""

    name = "trend"
    #: сколько закрытых свечей нужно бэктесту до первого решения
    _min_warmup = 300
    #: окно пересчёта индикаторов в бэктесте (EMA-200 успевает сойтись)
    _max_lookback = 600

    def __init__(self, params: TrendParams | None = None) -> None:
        self.params = params if params is not None else TrendParams()
        # память позиции для check_signal() (бэктест не знает про decide())
        self._bt_side: str = ""

    # ==================== индикаторы ====================

    def _snapshot(self, candles: list[Candle]) -> _Snapshot | None:
        """Посчитать индикаторы по окну свечей (None — данных мало)."""
        p = self.params
        if len(candles) < p.ema_macro + 2:
            return None
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        fast, slow, macro = (
            ema(closes, p.ema_fast),
            ema(closes, p.ema_slow),
            ema(closes, p.ema_macro),
        )
        adx_v, di_p, di_m = adx_di(highs, lows, closes, p.adx_period)
        st_line, st_dir = supertrend(highs, lows, closes, p.st_atr_period, p.st_factor)
        atr_v = atr(highs, lows, closes, p.atr_period)
        vol_ma = sma(volumes, p.vol_period)
        series = (fast, slow, macro, adx_v, di_p, di_m, st_line, st_dir, atr_v, vol_ma)
        if any(len(s) < 2 for s in series):
            return None

        close = closes[-1]
        f, s, m = fast[-1], slow[-1], macro[-1]
        return _Snapshot(
            close=close,
            trend_up=f > s and close > m,
            trend_down=f < s and close < m,
            adx=adx_v[-1],
            di_plus=di_p[-1],
            di_minus=di_m[-1],
            st_dir=st_dir[-1],
            st_flip=st_dir[-1] if st_dir[-1] != st_dir[-2] else 0,
            atr=atr_v[-1],
            vol_ratio=volumes[-1] / vol_ma[-1] if vol_ma[-1] > 0 else 0.0,
        )

    def _entry(self, snap: _Snapshot) -> str | None:
        """Сторона входа по свече (None — сигнала нет)."""
        p = self.params
        if snap.vol_ratio < p.vol_mult or snap.adx < p.adx_threshold:
            return None
        if snap.st_flip == 1 and snap.trend_up and snap.di_plus > snap.di_minus:
            return "long"
        if snap.st_flip == -1 and snap.trend_down and snap.di_minus > snap.di_plus:
            return "short"
        return None

    def _stop_take(self, snap: _Snapshot, side: str) -> tuple[float, float]:
        """SL/TP от ATR: стоп в 2×ATR, тейк в 3×ATR от текущей цены."""
        p = self.params
        if side == "long":
            return (
                snap.close - p.sl_atr_mult * snap.atr,
                snap.close + p.tp_atr_mult * snap.atr,
            )
        return (
            snap.close + p.sl_atr_mult * snap.atr,
            snap.close - p.tp_atr_mult * snap.atr,
        )

    def _exit_for(self, snap: _Snapshot, side: str) -> bool:
        """Supertrend развернулся против позиции — выход (трейлинг)."""
        return (side == "long" and snap.st_dir == -1) or (
            side == "short" and snap.st_dir == 1
        )

    # ==================== живой цикл ====================

    def decide(
        self,
        candles: list[Candle],
        position: OpenPosition | None,
    ) -> Instruction | None:
        """Решение живого цикла order_flow.

        Args:
            candles: окно свечей (старые → новые, последняя закрытая).
            position: открытая позиция или None.

        Returns:
            Instruction("enter"/"exit") либо None = hold.
        """
        snap = self._snapshot(candles)
        if snap is None:
            return None
        if position is not None:
            if self._exit_for(snap, position.side):
                return Instruction(action="exit", reason="supertrend развернулся")
            return None
        side = self._entry(snap)
        if side is None:
            return None
        stop, take = self._stop_take(snap, side)
        return Instruction(
            action="enter",
            side=side,
            stop=stop,
            take=take,
            reason=(f"ST flip {side}, ADX {snap.adx:.1f}, vol {snap.vol_ratio:.2f}x"),
        )

    # ==================== бэктест ====================

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Хук ``backtest.py --strategy trend``.

        Args:
            candles: свечи от старых к новым.

        Returns:
            Signal(buy/sell/close_long/close_short/hold) с абсолютными SL/TP.
        """
        snap = self._snapshot(candles)
        if snap is None:
            return Signal(action="hold", reason="мало свечей")
        if self._bt_side:
            if self._exit_for(snap, self._bt_side):
                close = "close_long" if self._bt_side == "long" else "close_short"
                self._bt_side = ""
                return Signal(
                    action=close, reason="supertrend развернулся", stop_loss=snap.close
                )
            return Signal(action="hold", reason="позиция открыта")
        side = self._entry(snap)
        if side is None:
            return Signal(action="hold", reason="нет сигнала")
        stop, take = self._stop_take(snap, side)
        self._bt_side = side
        return Signal(
            action="buy" if side == "long" else "sell",
            reason=f"ST flip {side}",
            stop_loss=stop,
            take_profit=take,
        )

    def on_position_closed(self, direction: str) -> None:
        """Сброс памяти позиции бэктеста (SL/TP закрыли сделку)."""
        self._bt_side = ""
