"""Стратегия robot_trend: вход на откате к EMA_FAST (без Supertrend).

HTF (старший ТФ) — фильтр направления и силы тренда: EMA + ADX/DI.
LTF (рабочий ТФ) — вход на откате к EMA_FAST с подтверждением объёмом.
Выход только по фиксированному стопу или тейку (SL / TAKE).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.indicators import adx_di, ema, sma
from core.strategies import BaseStrategy, Signal
from robot_trend.config import _env_bool, _env_float, _env_int
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
    """Параметры стратегии (из ``.env``)."""

    timeframe_min: int = 5
    htf_min: int = 60
    htf_warmup: int = 250
    ema_fast: int = 23
    ema_slow: int = 50
    ema_macro: int = 200
    adx_period: int = 14
    adx_threshold: float = 30.0
    sl_pct: float = 0.03
    take_pct: float = 0.10
    be_trigger_pct: float = 0.0
    be_offset_pct: float = 0.0
    trail_pct: float = 0.0
    trail_trigger_pct: float = 0.0
    partial_share: float = 0.0
    vol_period: int = 20
    vol_mult: float = 1.0
    ema_min_spread_pct: float = 0.1
    entry_on_pullback: bool = True

    @classmethod
    def from_env(cls, symbol: str | None = None) -> TrendParams:
        """Собрать параметры из окружения."""
        return cls(
            timeframe_min=_env_int("TIMEFRAME", 5),
            htf_min=_env_int("HTF_TIMEFRAME", 60),
            htf_warmup=_env_int("HTF_WARMUP", 250),
            ema_fast=_env_int("EMA_FAST", 23),
            ema_slow=_env_int("EMA_SLOW", 50),
            ema_macro=_env_int("EMA_MACRO", 200),
            adx_period=_env_int("ADX_PERIOD", 14),
            adx_threshold=_env_float("ADX_THRESHOLD", 30.0),
            sl_pct=_env_float("SL_PCT", 0.03),
            take_pct=_env_float("TAKE_PCT", 0.10),
            be_trigger_pct=_env_float("BE_TRIGGER", 0.0),
            be_offset_pct=_env_float("BE_OFFSET", 0.0),
            trail_pct=_env_float("TRAIL_PCT", 0.0),
            trail_trigger_pct=_env_float("TRAIL_TRIGGER", 0.0),
            partial_share=_env_float("PARTIAL_SHARE", 0.0),
            vol_period=_env_int("VOLUME_SMA_PERIOD", 20),
            vol_mult=_env_float("VOLUME_MULTIPLIER", 1.0),
            ema_min_spread_pct=_env_float("EMA_MIN_SPREAD_PCT", 0.1),
            entry_on_pullback=_env_bool("ENTRY_ON_PULLBACK", True),
        )

    def __post_init__(self) -> None:
        periods = {
            "EMA_FAST": self.ema_fast,
            "EMA_SLOW": self.ema_slow,
            "EMA_MACRO": self.ema_macro,
            "ADX_PERIOD": self.adx_period,
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
        if not 0 < self.sl_pct < 1 or not 0 <= self.take_pct < 1:
            raise ValueError("SL_PCT должен быть в (0, 1), TAKE_PCT — в [0, 1)")
        if min(self.be_trigger_pct, self.be_offset_pct, self.trail_pct) < 0:
            raise ValueError("BE_TRIGGER / BE_OFFSET / TRAIL_PCT не могут быть < 0")
        if self.trail_trigger_pct < 0:
            raise ValueError("TRAIL_TRIGGER не может быть < 0")
        if not 0 <= self.partial_share < 1:
            raise ValueError("PARTIAL_SHARE должен быть в [0, 1)")
        if self.vol_mult <= 0:
            raise ValueError("VOL_MULT должен быть > 0")
        if self.ema_min_spread_pct < 0:
            raise ValueError("EMA_MIN_SPREAD_PCT не может быть < 0")


@dataclass(frozen=True)
class _Snapshot:
    """Значения рабочего ТФ на последней закрытой свече."""

    close: float
    ema_fast: float
    ema_slow: float
    spread_pct: float  # зазор EMA_FAST / EMA_SLOW, %
    vol_ratio: float  # volume / SMA(volume)
    pullback_long: bool  # отскок от EMA_FAST снизу (лонг)
    pullback_short: bool  # отскок от EMA_FAST сверху (шорт)


@dataclass(frozen=True)
class _HTFSnapshot:
    """Значения старшего ТФ: направление и сила тренда."""

    trend_up: bool
    trend_down: bool
    adx: float
    di_plus: float
    di_minus: float


class TrendStrategy(BaseStrategy):
    """Тренд на откате к EMA_FAST: HTF-фильтр + LTF-отскок + объём.

    Вход LONG, когда одновременно:
      * HTF: EMA_FAST > EMA_SLOW, цена > EMA_MACRO, ADX >= порог, +DI > -DI
      * LTF: EMA_FAST > EMA_SLOW, зазор >= ema_min_spread_pct, объём >= vol_mult x SMA
      * Цена откатилась к EMA_FAST (предыдущая свеча дотронулась до него снизу)
        и отскочила (закрылась выше EMA_FAST).

    Вход SHORT — зеркально.

    Выход: только по серверному SL / TP. Стратегия не закрывает по своему сигналу.
    """

    name = "trend"
    _min_warmup = 300
    _max_lookback = 600

    def __init__(self, params: TrendParams | None = None) -> None:
        self.params = params if params is not None else TrendParams()
        # память позиции для check_signal() (бэктест не знает про decide())
        self._bt_side: str = ""
        # свечи старшего ТФ для check_signal() (кладёт backtest.py через set_htf)
        self._htf: list[Candle] = []

    # ==================== индикаторы ====================

    def _snapshot(self, candles: list[Candle]) -> _Snapshot | None:
        """Рабочий ТФ: EMA, объём, откат к EMA_FAST."""
        p = self.params
        need = max(p.ema_slow, p.vol_period) + 2
        if len(candles) < need:
            return None
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        volumes = [c.volume for c in candles]

        ema_fast = ema(closes, p.ema_fast)
        ema_slow = ema(closes, p.ema_slow)
        vol_ma = sma(volumes, p.vol_period)
        if len(ema_fast) < 2 or len(ema_slow) < 2 or len(vol_ma) < 2:
            return None

        close = closes[-1]
        f, s = ema_fast[-1], ema_slow[-1]
        spread_pct = abs(f - s) / s * 100.0 if s > 0 else 0.0
        vol_ratio = volumes[-1] / vol_ma[-1] if vol_ma[-1] > 0 else 0.0

        # Откат к EMA_FAST и отскок (на последней закрытой свече):
        # лонг: дно свечи коснулось EMA_FAST (откат к ней), закрытие выше EMA_FAST (отскок)
        pullback_long = lows[-1] <= f and close > f
        # шорт: верх свечи коснулся EMA_FAST, закрытие ниже EMA_FAST
        pullback_short = highs[-1] >= f and close < f

        return _Snapshot(
            close=close,
            ema_fast=f,
            ema_slow=s,
            spread_pct=spread_pct,
            vol_ratio=vol_ratio,
            pullback_long=pullback_long,
            pullback_short=pullback_short,
        )

    def _htf_snapshot(self, candles: list[Candle]) -> _HTFSnapshot | None:
        """Старший ТФ: направление (EMA) и сила (ADX/DI) тренда."""
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
        """Сторона входа: отскок от EMA_FAST в направлении тренда HTF."""
        p = self.params
        if ltf.vol_ratio < p.vol_mult:
            return None
        if htf.adx < p.adx_threshold:
            return None
        if p.ema_min_spread_pct > 0 and ltf.spread_pct < p.ema_min_spread_pct:
            return None

        if (
            p.entry_on_pullback
            and ltf.pullback_long
            and htf.trend_up
            and htf.di_plus > htf.di_minus
            and ltf.ema_fast > ltf.ema_slow
        ):
            return "long"
        if (
            p.entry_on_pullback
            and ltf.pullback_short
            and htf.trend_down
            and htf.di_minus > htf.di_plus
            and ltf.ema_fast < ltf.ema_slow
        ):
            return "short"
        return None

    def _stop_take(self, ltf: _Snapshot, side: str) -> tuple[float, float]:
        """SL/TP от цены сигнала; TAKE_PCT=0 отключает фиксированный тейк."""
        p = self.params
        take = (
            0.0
            if p.take_pct == 0
            else ltf.close * (1.0 + p.take_pct if side == "long" else 1.0 - p.take_pct)
        )
        if side == "long":
            return (ltf.close * (1.0 - p.sl_pct), take)
        return (ltf.close * (1.0 + p.sl_pct), take)

    def _exit_for(self, ltf: _Snapshot, side: str) -> bool:
        """Выход только по стопу/тейку (биржевой). Стратегия не закрывает по сигналу."""
        return False

    # ==================== живой цикл ====================

    def decide(
        self,
        candles: list[Candle],
        htf_candles: list[Candle],
        position: OpenPosition | None,
    ) -> Instruction | None:
        """Решение живого цикла order_flow.

        При открытой позиции возвращает None — выход только по серверному SL/TP.
        """
        if position is not None:
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
            reason=(
                f"EMA pullback {side}, HTF ADX {htf.adx:.1f}, "
                f"vol {ltf.vol_ratio:.2f}x, spread {ltf.spread_pct:.2f}%"
            ),
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
        return closed[-p.htf_warmup :]

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Хук ``backtest.py --strategy trend``."""
        if not candles:
            return Signal(action="hold", reason="мало свечей")
        ltf = self._snapshot(candles)
        if ltf is None:
            return Signal(action="hold", reason="мало свечей")
        if self._bt_side:
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
            reason=f"EMA pullback {side}",
            stop_loss=stop,
            take_profit=take,
        )

    def on_position_closed(self, direction: str) -> None:
        """Сброс памяти позиции бэктеста (SL/TP закрыли сделку)."""
        self._bt_side = ""
