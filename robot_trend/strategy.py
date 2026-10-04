"""Стратегия robot_trend: мульти-ТФ — 30м (режим) + 5м (триггер).

| # | Индикатор      | ТФ  | Параметры (.env)                | Роль                          |
|---|----------------|-----|---------------------------------|-------------------------------|
| 1 | EMA            | 30м | 20 / 50 / 200                   | направление тренда            |
| 2 | ADX (+DI/-DI)  | 30м | period=14, threshold=25         | сила тренда / допустимая сторона |
| 3 | Supertrend     | 5м  | ATR=10, factor=3.0 (альты);     | триггер входа + трейлинг-стоп |
|   |                |     | ATR=55, factor=2.0 (BTC/ETH)    |                               |
| 4 | Volume SMA     | 5м  | period=20, multiplier=1.3       | подтверждение пробоя          |

Вход (5м флип Supertrend + объём) только в направлении и при силе тренда
старшего ТФ (EMA + ADX/DI на 30м). Выход: разворот Supertrend на 5м против
позиции (серверный SL/TP — страховка).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core.bybit_client import Candle
from core.indicators import adx_di, ema, sma, supertrend
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
    """Параметры стратегии (из ``.env``, дефолты из таблицы в docstring)."""

    timeframe_min: int = 5  # рабочий ТФ (минуты) — триггер входа
    htf_min: int = 30  # старший ТФ (минуты) — направление + сила
    htf_warmup: int = 250  # сколько свечей старшего ТФ держим/грузим
    ema_fast: int = 20
    ema_slow: int = 50
    ema_macro: int = 200
    adx_period: int = 14
    adx_threshold: float = 25.0
    st_atr_period: int = 10
    st_factor: float = 3.0
    sl_pct: float = 0.02  # стоп в % от входа
    tp_pct: float = 0.05  # тейк в % от входа
    be_trigger_pct: float = 0.02  # BE/trail включаются после +2%
    be_offset_pct: float = 0.0  # уровень безубытка = вход + offset
    trail_pct: float = 0.02  # трейлинг: стоп = пик - 2%
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
            timeframe_min=_env_int("TIMEFRAME", 5),
            htf_min=_env_int("HTF_TIMEFRAME", 30),
            htf_warmup=_env_int("HTF_WARMUP", 250),
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
            sl_pct=_env_float("SL_PCT", 0.02),
            tp_pct=_env_float("TP_PCT", 0.05),
            be_trigger_pct=_env_float("BE_TRIGGER", 0.02),
            be_offset_pct=_env_float("BE_OFFSET", 0.0),
            trail_pct=_env_float("TRAIL_PCT", 0.02),
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
            "VOL_PERIOD": self.vol_period,
        }
        for name, value in periods.items():
            if value < 2:
                raise ValueError(f"{name} должен быть >= 2")
        if not self.ema_fast < self.ema_slow < self.ema_macro:
            raise ValueError("нужен порядок EMA_FAST < EMA_SLOW < EMA_MACRO")
        if self.timeframe_min < 1 or self.htf_min <= self.timeframe_min:
            raise ValueError("нужен TIMEFRAME >= 1 и HTF_TIMEFRAME > TIMEFRAME")
        if self.htf_warmup < self.ema_macro + 2:
            raise ValueError("HTF_WARMUP должен быть >= EMA_MACRO + 2")
        if self.adx_threshold < 0:
            raise ValueError("ADX_THRESHOLD не может быть < 0")
        if self.st_factor <= 0:
            raise ValueError("ST_FACTOR должен быть > 0")
        if not 0 < self.sl_pct < 1 or not 0 < self.tp_pct < 1:
            raise ValueError("SL_PCT / TP_PCT должны быть в (0, 1)")
        if min(self.be_trigger_pct, self.be_offset_pct, self.trail_pct) < 0:
            raise ValueError("BE_TRIGGER / BE_OFFSET / TRAIL_PCT не могут быть < 0")
        if self.vol_mult <= 0:
            raise ValueError("VOL_MULT должен быть > 0")


@dataclass(frozen=True)
class _Snapshot:
    """Значения 5м-индикаторов на последней закрытой свече."""

    close: float
    st_dir: int  # +1 / -1
    st_flip: int  # смена направления Supertrend на этой свече, иначе 0
    vol_ratio: float  # volume / SMA(volume)


@dataclass(frozen=True)
class _HTFSnapshot:
    """Значения старшего ТФ (30м): направление и сила тренда."""

    trend_up: bool  # fast > slow и close > macro
    trend_down: bool
    adx: float
    di_plus: float
    di_minus: float


class TrendStrategy(BaseStrategy):
    """Мульти-ТФ: режим по 30м (EMA + ADX/DI), вход по 5м (ST + объём)."""

    name = "trend"
    #: сколько закрытых свечей нужно бэктесту до первого решения
    _min_warmup = 300
    #: окно пересчёта индикаторов в бэктесте
    _max_lookback = 600

    def __init__(self, params: TrendParams | None = None) -> None:
        self.params = params if params is not None else TrendParams()
        # память позиции для check_signal() (бэктест не знает про decide())
        self._bt_side: str = ""
        # свечи старшего ТФ для check_signal() (кладёт backtest.py через set_htf)
        self._htf: list[Candle] = []

    # ==================== индикаторы ====================

    def _snapshot(self, candles: list[Candle]) -> _Snapshot | None:
        """Свечи рабочего ТФ: Supertrend и объём (None — данных мало)."""
        p = self.params
        if len(candles) < max(p.st_atr_period, p.vol_period) + 2:
            return None
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        _, st_dir = supertrend(highs, lows, closes, p.st_atr_period, p.st_factor)
        vol_ma = sma(volumes, p.vol_period)
        if len(st_dir) < 2 or len(vol_ma) < 2:
            return None

        return _Snapshot(
            close=closes[-1],
            st_dir=st_dir[-1],
            st_flip=st_dir[-1] if st_dir[-1] != st_dir[-2] else 0,
            vol_ratio=volumes[-1] / vol_ma[-1] if vol_ma[-1] > 0 else 0.0,
        )

    def _htf_snapshot(self, candles: list[Candle]) -> _HTFSnapshot | None:
        """Свечи старшего ТФ: направление (EMA) и сила (ADX/DI) тренда."""
        p = self.params
        if len(candles) < p.ema_macro + 2:
            return None
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        fast, slow, macro = (
            ema(closes, p.ema_fast),
            ema(closes, p.ema_slow),
            ema(closes, p.ema_macro),
        )
        adx_v, di_p, di_m = adx_di(highs, lows, closes, p.adx_period)
        series = (fast, slow, macro, adx_v, di_p, di_m)
        if any(len(s) < 2 for s in series):
            return None

        close = closes[-1]
        f, s, m = fast[-1], slow[-1], macro[-1]
        return _HTFSnapshot(
            trend_up=f > s and close > m,
            trend_down=f < s and close < m,
            adx=adx_v[-1],
            di_plus=di_p[-1],
            di_minus=di_m[-1],
        )

    def _entry(self, ltf: _Snapshot, htf: _HTFSnapshot) -> str | None:
        """Сторона входа: триггер 5м в направлении/силе тренда 30м."""
        p = self.params
        if ltf.vol_ratio < p.vol_mult or htf.adx < p.adx_threshold:
            return None
        if ltf.st_flip == 1 and htf.trend_up and htf.di_plus > htf.di_minus:
            return "long"
        if ltf.st_flip == -1 and htf.trend_down and htf.di_minus > htf.di_plus:
            return "short"
        return None

    def _stop_take(self, ltf: _Snapshot, side: str) -> tuple[float, float]:
        """SL/TP в процентах от цены входа (close свечи сигнала 5м)."""
        p = self.params
        if side == "long":
            return (ltf.close * (1.0 - p.sl_pct), ltf.close * (1.0 + p.tp_pct))
        return (ltf.close * (1.0 + p.sl_pct), ltf.close * (1.0 - p.tp_pct))

    def _exit_for(self, ltf: _Snapshot, side: str) -> bool:
        """Supertrend на 5м развернулся против позиции — выход (трейлинг)."""
        return (side == "long" and ltf.st_dir == -1) or (
            side == "short" and ltf.st_dir == 1
        )

    # ==================== живой цикл ====================

    def decide(
        self,
        candles: list[Candle],
        htf_candles: list[Candle],
        position: OpenPosition | None,
    ) -> Instruction | None:
        """Решение живого цикла order_flow.

        Args:
            candles: свечи рабочего ТФ (старые → новые, последняя закрытая).
            htf_candles: закрытые свечи старшего ТФ.
            position: открытая позиция или None.

        Returns:
            Instruction("enter"/"exit") либо None = hold.
        """
        if position is not None:
            # выход не зависит от старшего ТФ — страховка работает всегда
            ltf = self._snapshot(candles)
            if ltf is not None and self._exit_for(ltf, position.side):
                return Instruction(action="exit", reason="supertrend развернулся")
            return None
        ltf = self._snapshot(candles)
        htf = self._htf_snapshot(htf_candles)
        if ltf is None or htf is None:
            return None
        side = self._entry(ltf, htf)
        if side is None:
            return None
        stop, take = self._stop_take(ltf, side)
        return Instruction(
            action="enter",
            side=side,
            stop=stop,
            take=take,
            reason=(f"ST flip {side}, HTF ADX {htf.adx:.1f}, vol {ltf.vol_ratio:.2f}x"),
        )

    # ==================== бэктест ====================

    def set_htf(self, htf_candles: list[Candle]) -> None:
        """Отдать стратегии свечи старшего ТФ (кладёт backtest.py)."""
        self._htf = list(htf_candles)

    def _closed_htf(self, bar: Candle) -> list[Candle]:
        """Свечи старшего ТФ, закрытые к моменту закрытия ``bar`` (без hindsight)."""
        p = self.params
        deadline = bar.open_time + p.timeframe_min * 60_000
        closed = [c for c in self._htf if c.open_time + p.htf_min * 60_000 <= deadline]
        # то же окно, что видит live (HTF_WARMUP последних закрытых свечей)
        return closed[-p.htf_warmup :]

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Хук ``backtest.py --strategy trend``.

        Args:
            candles: свечи рабочего ТФ от старых к новым.

        Returns:
            Signal(buy/sell/close_long/close_short/hold) с абсолютными SL/TP.
        """
        if not candles:
            return Signal(action="hold", reason="мало свечей")
        ltf = self._snapshot(candles)
        if ltf is None:
            return Signal(action="hold", reason="мало свечей")
        if self._bt_side:
            if self._exit_for(ltf, self._bt_side):
                close = "close_long" if self._bt_side == "long" else "close_short"
                self._bt_side = ""
                return Signal(
                    action=close, reason="supertrend развернулся", stop_loss=ltf.close
                )
            return Signal(action="hold", reason="позиция открыта")
        htf = self._htf_snapshot(self._closed_htf(candles[-1]))
        if htf is None:
            return Signal(action="hold", reason="мало свечей старшего ТФ")
        side = self._entry(ltf, htf)
        if side is None:
            return Signal(action="hold", reason="нет сигнала")
        stop, take = self._stop_take(ltf, side)
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
