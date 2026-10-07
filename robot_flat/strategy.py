"""Стратегия robot_flat: EMA 20/50/200 + ADX 14 + ATR 14 (4H).

| # | Индикатор | Параметры (.env)     | Роль                                  |
|---|-----------|----------------------|---------------------------------------|
| 1 | EMA       | 20/50/200            | направление и режим (must-have)       |
| 2 | ADX       | период 14, порог 25  | фильтр: тренд или флэт (must-have)    |
| 3 | ATR       | период 14, ×1.5 / ×3 | стоп и тейк (must-have)               |
| + | RSI       | 14, ≥50 / <50        | опциональный фильтр (RSI_FILTER)      |
| + | Volume    | SMA20 × 1.3          | опциональный фильтр (VOLUME_FILTER)   |

Вход по **состоянию**: EMA20 > EMA50, close > EMA200 и ADX >= 25 (long;
зеркально short) + опциональные RSI/Volume-фильтры. Выход: разворот
состояния (EMA20/50 или close против EMA200) либо срабатывание
ATR-стопа/тейка (SL/TP считает бэктест/биржа).

Правило внедрения индикаторов: каждый новый индикатор включается флагом
только после того, как улучшил PF на прогоне (см. SESSION_NOTES).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.indicators import adx_di, atr, ema, rsi, sma
from core.strategies import BaseStrategy, Signal
from robot_flat.config import _env_bool, _env_float, _env_int
from robot_flat.state import OpenPosition


@dataclass(frozen=True)
class Instruction:
    """Решение стратегии. Исполняет технический слой (order_flow)."""

    action: str  # "enter" | "exit"
    side: str = ""  # "long" | "short" (для enter)
    stop: float | None = None  # уровень SL (для enter)
    take: float | None = None  # уровень TP (для enter)
    reason: str = ""


@dataclass(frozen=True)
class FlatParams:
    """Параметры стратегии (из ``.env``, дефолты из таблицы в docstring)."""

    # --- must-have: EMA (режим/направление) ---
    ema_fast: int = 20
    ema_slow: int = 50
    ema_macro: int = 200
    # --- must-have: ADX (тренд против флэта) ---
    adx_period: int = 14
    adx_threshold: float = 25.0
    # --- must-have: ATR (стоп/тейк) ---
    atr_period: int = 14
    atr_sl_mult: float = 1.5  # SL = k x ATR от цены входа
    atr_tp_mult: float = 3.0  # TP = k x ATR (RR 1:2)
    # --- опциональные фильтры (включаются флагом после проверки PF) ---
    use_rsi: bool = False
    rsi_period: int = 14
    rsi_side: float = 50.0  # long: RSI >= 50, short: RSI < 50
    use_volume: bool = False
    vol_period: int = 20
    vol_mult: float = 1.3  # volume >= 1.3 x SMA20
    # --- риск-слой (BE/trail) выключен: стопы считает ATR ---
    sl_pct: float = 0.0
    be_trigger_pct: float = 0.0
    be_offset_pct: float = 0.0
    trail_pct: float = 0.0

    @classmethod
    def from_env(cls) -> FlatParams:
        """Собрать параметры из окружения (RSI_FILTER / VOLUME_FILTER — флаги)."""
        return cls(
            ema_fast=_env_int("EMA_FAST", 20),
            ema_slow=_env_int("EMA_SLOW", 50),
            ema_macro=_env_int("EMA_MACRO", 200),
            adx_period=_env_int("ADX_PERIOD", 14),
            adx_threshold=_env_float("ADX_THRESHOLD", 25.0),
            atr_period=_env_int("ATR_PERIOD", 14),
            atr_sl_mult=_env_float("ATR_SL_MULT", 1.5),
            atr_tp_mult=_env_float("ATR_TP_MULT", 3.0),
            use_rsi=_env_bool("RSI_FILTER", False),
            rsi_period=_env_int("RSI_PERIOD", 14),
            use_volume=_env_bool("VOLUME_FILTER", False),
            vol_period=_env_int("VOL_PERIOD", 20),
            vol_mult=_env_float("VOL_MULT", 1.3),
        )

    def __post_init__(self) -> None:
        periods = {
            "EMA_FAST": self.ema_fast,
            "EMA_SLOW": self.ema_slow,
            "EMA_MACRO": self.ema_macro,
            "ADX_PERIOD": self.adx_period,
            "ATR_PERIOD": self.atr_period,
            "RSI_PERIOD": self.rsi_period,
            "VOL_PERIOD": self.vol_period,
        }
        for name, value in periods.items():
            if value < 2:
                raise ValueError(f"{name} должен быть >= 2")
        if not self.ema_fast < self.ema_slow < self.ema_macro:
            raise ValueError("нужен порядок EMA_FAST < EMA_SLOW < EMA_MACRO")
        if self.adx_threshold < 0:
            raise ValueError("ADX_THRESHOLD не может быть < 0")
        if self.atr_sl_mult <= 0 or self.atr_tp_mult <= 0:
            raise ValueError("ATR_SL_MULT / ATR_TP_MULT должны быть > 0")
        if not 0 <= self.sl_pct < 1:
            raise ValueError("SL_PCT должен быть в [0, 1)")
        if min(self.be_trigger_pct, self.be_offset_pct, self.trail_pct) < 0:
            raise ValueError("BE_TRIGGER / BE_OFFSET / TRAIL_PCT не могут быть < 0")
        if not 0 < self.rsi_side < 100:
            raise ValueError("RSI_SIDE должен быть в (0, 100)")
        if self.vol_mult <= 0:
            raise ValueError("VOL_MULT должен быть > 0")


@dataclass(frozen=True)
class _Snapshot:
    """Значения индикаторов на последней закрытой свече."""

    close: float
    ema_fast: float
    ema_slow: float
    ema_macro: float
    adx: float
    atr: float
    rsi_v: float
    vol_ratio: float  # volume / SMA20(volume)


class FlatStrategy(BaseStrategy):
    """EMA 20/50/200 + ADX 14/25 + ATR 14; опционально RSI и Volume."""

    name = "flat"
    #: сколько закрытых свечей нужно бэктесту до первого решения
    _min_warmup = 300
    #: окно пересчёта индикаторов в бэктесте
    _max_lookback = 600

    def __init__(self, params: FlatParams | None = None) -> None:
        self.params = params if params is not None else FlatParams.from_env()
        # память позиции для check_signal() (бэктест не знает про decide())
        self._bt_side: str = ""

    # ==================== индикаторы ====================

    def _snapshot(self, candles: list[Candle]) -> _Snapshot | None:
        """Свечи рабочего ТФ: EMA/ADX/ATR/RSI/объём (None — данных мало)."""
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
        adx_v, _, _ = adx_di(highs, lows, closes, p.adx_period)
        atr_v = atr(highs, lows, closes, p.atr_period)
        rsi_v = rsi(closes, p.rsi_period)
        vol_ma = sma(volumes, p.vol_period)
        series = (fast, slow, macro, adx_v, atr_v, rsi_v, vol_ma)
        if any(len(s) < 2 for s in series):
            return None
        if atr_v[-1] <= 0:
            return None

        return _Snapshot(
            close=closes[-1],
            ema_fast=fast[-1],
            ema_slow=slow[-1],
            ema_macro=macro[-1],
            adx=adx_v[-1],
            atr=atr_v[-1],
            rsi_v=rsi_v[-1],
            vol_ratio=volumes[-1] / vol_ma[-1] if vol_ma[-1] > 0 else 0.0,
        )

    # ==================== решения ====================

    def _entry_side(self, s: _Snapshot) -> str | None:
        """Сторона входа: состояние EMA (направление/режим) + ADX (тренд)."""
        p = self.params
        if s.adx < p.adx_threshold:
            return None
        side: str | None = None
        if s.ema_fast > s.ema_slow and s.close > s.ema_macro:
            side = "long"
        elif s.ema_fast < s.ema_slow and s.close < s.ema_macro:
            side = "short"
        if side is None:
            return None
        if p.use_rsi:
            long_ok = s.rsi_v >= p.rsi_side
            if (side == "long") != long_ok:
                return None
        if p.use_volume and s.vol_ratio < p.vol_mult:
            return None
        return side

    def _exit_for(self, s: _Snapshot, side: str) -> bool:
        """Разворот состояния EMA против позиции — выход (SL/TP делает биржа)."""
        if side == "long":
            return s.ema_fast < s.ema_slow or s.close < s.ema_macro
        return s.ema_fast > s.ema_slow or s.close > s.ema_macro

    def _stop_take(self, s: _Snapshot, side: str) -> tuple[float, float]:
        """ATR-стоп и тейк (RR = atr_tp_mult / atr_sl_mult)."""
        p = self.params
        if side == "long":
            return (s.close - p.atr_sl_mult * s.atr, s.close + p.atr_tp_mult * s.atr)
        return (s.close + p.atr_sl_mult * s.atr, s.close - p.atr_tp_mult * s.atr)

    # ==================== живой цикл ====================

    def decide(
        self,
        candles: list[Candle],
        position: OpenPosition | None,
    ) -> Instruction | None:
        """Решение живого цикла order_flow.

        Args:
            candles: свечи рабочего ТФ (старые → новые, последняя закрытая).
            position: открытая позиция или None.

        Returns:
            Instruction("enter"/"exit") либо None = hold.
        """
        s = self._snapshot(candles)
        if s is None:
            return None
        if position is not None:
            if self._exit_for(s, position.side):
                return Instruction(action="exit", reason="разворот EMA")
            return None
        side = self._entry_side(s)
        if side is None:
            return None
        stop, take = self._stop_take(s, side)
        return Instruction(
            action="enter",
            side=side,
            stop=stop,
            take=take,
            reason=(
                f"EMA state {side}, ADX {s.adx:.1f}, ATR {s.atr:.4f}"
                + (f", RSI {s.rsi_v:.0f}" if self.params.use_rsi else "")
            ),
        )

    # ==================== бэктест ====================

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Хук ``backtest.py --strategy flat``.

        Args:
            candles: свечи рабочего ТФ от старых к новым.

        Returns:
            Signal(buy/sell/close_long/close_short/hold) с абсолютными SL/TP.
        """
        if not candles:
            return Signal(action="hold", reason="нет свечей")
        s = self._snapshot(candles)
        if s is None:
            return Signal(action="hold", reason="мало свечей")
        if self._bt_side:
            if self._exit_for(s, self._bt_side):
                close = "close_long" if self._bt_side == "long" else "close_short"
                self._bt_side = ""
                return Signal(action=close, reason="разворот EMA", stop_loss=s.close)
            return Signal(action="hold", reason="позиция открыта")
        side = self._entry_side(s)
        if side is None:
            return Signal(action="hold", reason="нет сигнала")
        stop, take = self._stop_take(s, side)
        self._bt_side = side
        return Signal(
            action="buy" if side == "long" else "sell",
            reason=f"EMA state {side}",
            stop_loss=stop,
            take_profit=take,
        )

    def on_position_closed(self, direction: str) -> None:
        """Сброс памяти позиции бэктеста (SL/TP закрыли сделку)."""
        self._bt_side = ""
