"""Стратегия robot_flat v3: сетка вокруг POC внутри VA-диапазона (M5).

| # | Правило | Параметры (.env) |
|---|---------|------------------|
| 1 | Профиль: POC + VAH/VAL (value area 70%) из последних 30 мин | POC_WINDOW_MIN, POC_REFRESH_MIN, VALUE_AREA_PCT |
| 2 | Пересчёт профиля каждый час (rolling) + форс по халту | POC_REFRESH_MIN |
| 3 | Тренд-гейт: ADX >= порога → новые входы запрещены | ADX_PERIOD, ADX_THRESHOLD |
| 4 | Середина (MIDDLE_PCT ширины вокруг POC) — не торгуется | MIDDLE_PCT, GRID_LEVELS |
| 5 | Вход: касание уровня сетки (лимит-эмуляция) | — |
| 6 | SL = граница VA ± 5% цены | BREAKOUT_PCT |
| 7 | TP1 (50%) = POC; TP2 (50%) = ближайший уровень обратной сетки − 1% к POC | TP_SPLIT, TP_OFFSET_PCT |
| 8 | BE: +3% от входа → стоп = вход (считают backtest/risk) | BE_TRIGGER_PCT |
| 9 | Гвард: вне диапазона > 10 мин → халт входов + форс-пересчёт POC | BREAKOUT_RETURN_MIN |

Сетка: 3 уровня в каждом секторе [VAL..POC−зона] и [POC+зона..VAH],
равномерно, только внутри VA. Сторона занята, пока позиции не закрыты
полностью (on_position_closed) — максимум GRID_LEVELS одновременно.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.bybit_client import Candle
from core.indicators import adx_di
from core.strategies import BaseStrategy, Signal
from core.volume_profile import compute_poc_va
from robot_flat.config import _env_float, _env_int
from robot_flat.state import OpenPosition

MS_MIN = 60_000


@dataclass(frozen=True)
class Instruction:
    """Решение стратегии. Исполняет технический слой (order_flow)."""

    action: str  # "enter" | "exit"
    side: str = ""  # "long" | "short" (для enter)
    price: float | None = None  # уровень входа — цена лимитки (для enter)
    stop: float | None = None  # уровень SL (для enter)
    take: float | None = None  # уровень TP (для enter)
    reason: str = ""


@dataclass(frozen=True)
class FlatParams:
    """Параметры стратегии (из ``.env``, дефолты из таблицы в docstring)."""

    # --- профиль ---
    poc_window_min: int = 30
    poc_refresh_min: int = 60
    value_area_pct: float = 0.7
    profile_bins: int = 100
    # --- тренд-гейт ---
    adx_period: int = 14
    adx_threshold: float = 25.0
    # --- сетка ---
    grid_levels: int = 3
    middle_pct: float = 0.5  # запрет: middle_pct ширины VA вокруг POC
    # --- стоп/тейк ---
    breakout_pct: float = 0.05  # SL = граница VA ± 5% цены
    breakout_return_min: int = 10  # гвард: возврат в диапазон за 10 мин
    tp_offset_pct: float = 0.01  # TP2: на 1% ближе к POC
    tp_split: float = 0.5  # доля позиции на TP1 (POC)
    # --- риск-слой (читают backtest.py / risk.on_price) ---
    sl_pct: float = 0.0  # стоп считает стратегия (граница ±5%)
    be_trigger_pct: float = 0.03  # BE через 3% от входа
    be_offset_pct: float = 0.0  # стоп BE = вход
    trail_pct: float = 0.0

    @classmethod
    def from_env(cls) -> FlatParams:
        """Собрать параметры из окружения."""
        return cls(
            poc_window_min=_env_int("POC_WINDOW_MIN", 30),
            poc_refresh_min=_env_int("POC_REFRESH_MIN", 60),
            value_area_pct=_env_float("VALUE_AREA_PCT", 0.7),
            profile_bins=_env_int("PROFILE_BINS", 100),
            adx_period=_env_int("ADX_PERIOD", 14),
            adx_threshold=_env_float("ADX_THRESHOLD", 25.0),
            grid_levels=_env_int("GRID_LEVELS", 3),
            middle_pct=_env_float("MIDDLE_PCT", 0.5),
            breakout_pct=_env_float("BREAKOUT_PCT", 0.05),
            breakout_return_min=_env_int("BREAKOUT_RETURN_MIN", 10),
            tp_offset_pct=_env_float("TP_OFFSET_PCT", 0.01),
            tp_split=_env_float("TP_SPLIT", 0.5),
            be_trigger_pct=_env_float("BE_TRIGGER_PCT", 0.03),
        )

    def __post_init__(self) -> None:
        ints = {
            "POC_WINDOW_MIN": self.poc_window_min,
            "POC_REFRESH_MIN": self.poc_refresh_min,
            "PROFILE_BINS": self.profile_bins,
            "ADX_PERIOD": self.adx_period,
            "GRID_LEVELS": self.grid_levels,
            "BREAKOUT_RETURN_MIN": self.breakout_return_min,
        }
        for name, value in ints.items():
            if value < 1:
                raise ValueError(f"{name} должен быть >= 1")
        if not 0 < self.value_area_pct <= 1:
            raise ValueError("VALUE_AREA_PCT должен быть в (0, 1]")
        if self.adx_threshold < 0:
            raise ValueError("ADX_THRESHOLD не может быть < 0")
        if not 0 < self.middle_pct < 1:
            raise ValueError("MIDDLE_PCT должен быть в (0, 1)")
        if self.breakout_pct <= 0:
            raise ValueError("BREAKOUT_PCT должен быть > 0")
        if not 0 <= self.tp_offset_pct < 1:
            raise ValueError("TP_OFFSET_PCT должен быть в [0, 1)")
        if not 0 < self.tp_split < 1:
            raise ValueError("TP_SPLIT должен быть в (0, 1)")
        if not 0 <= self.sl_pct < 1:
            raise ValueError("SL_PCT должен быть в [0, 1)")
        if min(self.be_trigger_pct, self.be_offset_pct, self.trail_pct) < 0:
            raise ValueError("BE_TRIGGER / BE_OFFSET / TRAIL_PCT не могут быть < 0")


@dataclass(frozen=True)
class _Profile:
    """POC/VA на момент пересчёта (ts = open_time свечи)."""

    ts: int
    poc: float
    val: float
    vah: float


@dataclass(frozen=True)
class _Decision:
    """Внутреннее решение _eval (hold либо вход по уровню)."""

    kind: str  # "hold" | "enter"
    side: str = ""
    level: float = 0.0
    stop: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    reason: str = ""


class FlatStrategy(BaseStrategy):
    """Сетка вокруг POC в VA-диапазоне; ADX-гейт, гвард 5%/10 мин."""

    name = "flat"
    #: сколько закрытых свечей нужно бэктесту до первого решения
    _min_warmup = 60
    #: окно индикаторов (ADX) в бэктесте
    _max_lookback = 120

    def __init__(self, params: FlatParams | None = None) -> None:
        self.params = params if params is not None else FlatParams.from_env()
        self._profile: _Profile | None = None
        self._breakout_since: int | None = None
        # занятые уровни по сторонам (сбрасываются при опустошении стороны)
        self._book: dict[str, set[float]] = {"long": set(), "short": set()}

    # ==================== профиль и сетка ====================

    def _refresh(self, candles: list[Candle], t: int) -> bool:
        """Пересчитать профиль из последних POC_WINDOW_MIN минут.

        Returns:
            True — профиль есть (или старый остался); False — данных нет.
        """
        window_ms = self.params.poc_window_min * MS_MIN
        recent = [c for c in candles if c.open_time > t - window_ms]
        levels = compute_poc_va(
            recent,
            num_bins=self.params.profile_bins,
            pct=self.params.value_area_pct,
        )
        if levels is None:
            return self._profile is not None
        poc, val, vah = levels
        self._profile = _Profile(ts=t, poc=poc, val=val, vah=vah)
        self._breakout_since = None
        return True

    def _grid(self, p: _Profile) -> tuple[list[float], list[float]]:
        """Уровни сетки: (longs, shorts) по возрастанию цены.

        Секторы: [VAL..POC−зона] и [POC+зона..VAH]; middle_pct ширины
        вокруг POC не торгуется; уровни равномерно, строго внутри VA.
        """
        n = self.params.grid_levels
        width = p.vah - p.val
        if width <= 0:
            return [], []
        half_middle = width * self.params.middle_pct / 2.0
        longs: list[float] = []
        shorts: list[float] = []
        low_top = p.poc - half_middle
        if low_top > p.val:
            span = low_top - p.val
            longs = [p.val + span * k / (n + 1) for k in range(1, n + 1)]
        up_bottom = p.poc + half_middle
        if p.vah > up_bottom:
            span = p.vah - up_bottom
            shorts = [up_bottom + span * k / (n + 1) for k in range(1, n + 1)]
        return longs, shorts

    # ==================== ядро решений ====================

    def _eval(self, candles: list[Candle]) -> _Decision:
        """Общая логика для check_signal (бэктест) и decide (живой цикл).

        Сайд-эффект: при входе уровень добавляется в книгу стороны.
        """
        p = self.params
        if len(candles) < max(p.adx_period * 2, 12):
            return _Decision("hold", reason="мало свечей")
        bar = candles[-1]
        t = bar.open_time
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        adx_s, _, _ = adx_di(highs, lows, closes, p.adx_period)
        if len(adx_s) < 2:
            return _Decision("hold", reason="ADX не посчитался")
        adx = adx_s[-1]

        # 1) часовой rolling-пересчёт профиля
        need_refresh = self._profile is None or (
            t - self._profile.ts >= p.poc_refresh_min * MS_MIN
        )
        if need_refresh and not self._refresh(candles, t):
            return _Decision("hold", reason="профиль не построился")
        prof = self._profile
        if prof is None:
            return _Decision("hold", reason="нет профиля")

        # 2) гвард: вне диапазона дольше лимита → халт + форс-пересчёт
        outside = bar.close < prof.val or bar.close > prof.vah
        if outside:
            if self._breakout_since is None:
                self._breakout_since = t
            elif t - self._breakout_since >= p.breakout_return_min * MS_MIN:
                self._refresh(candles, t)
                return _Decision(
                    "hold",
                    reason="халт: вне диапазона > 10м, POC пересчитан",
                )
            # входов вне VA нет — ждём возврата или халта
            return _Decision("hold", reason=f"вне диапазона, ADX {adx:.1f}")
        self._breakout_since = None

        # 3) тренд-гейт: во флейте не торгуемся
        if adx >= p.adx_threshold:
            return _Decision(
                "hold",
                reason=f"тренд: ADX {adx:.1f} >= {p.adx_threshold}",
            )

        # 4) сетка: касание свободного уровня (ближайший к POC)
        longs, shorts = self._grid(prof)
        side = ""
        level = 0.0
        long_cands = [
            lv
            for lv in longs
            if lv not in self._book["long"] and bar.low <= lv <= bar.high
        ]
        if long_cands and len(self._book["long"]) < p.grid_levels:
            side, level = "long", max(long_cands)  # ближе к POC = выше
        else:
            short_cands = [
                lv
                for lv in shorts
                if lv not in self._book["short"] and bar.low <= lv <= bar.high
            ]
            if short_cands and len(self._book["short"]) < p.grid_levels:
                side, level = "short", min(short_cands)  # ближе к POC = ниже
        if not side:
            if (
                len(self._book["long"]) >= p.grid_levels
                or len(self._book["short"]) >= p.grid_levels
            ):
                return _Decision("hold", reason="сетка стороны заполнена")
            return _Decision("hold", reason=f"уровней не коснулись, ADX {adx:.1f}")

        # 5) стоп/тейки: SL за границей VA, TP1 = POC,
        #    TP2 = ближайший уровень ОБРАТНОЙ сетки, сдвинутый к POC
        if side == "long":
            stop = prof.val * (1.0 - p.breakout_pct)
            tp1 = prof.poc
            tp2 = tp1
            if shorts:
                tp2 = max(min(shorts) * (1.0 - p.tp_offset_pct), tp1)
        else:
            stop = prof.vah * (1.0 + p.breakout_pct)
            tp1 = prof.poc
            tp2 = tp1
            if longs:
                tp2 = min(max(longs) * (1.0 + p.tp_offset_pct), tp1)
        self._book[side].add(level)
        return _Decision(
            "enter",
            side=side,
            level=level,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            reason=(
                f"grid {side} @ {level:.6g}, ADX {adx:.1f}, "
                f"POC {prof.poc:.6g} [{prof.val:.6g}..{prof.vah:.6g}]"
            ),
        )

    # ==================== живой цикл ====================

    def decide(
        self,
        candles: list[Candle],
        position: OpenPosition | None,
    ) -> Instruction | None:
        """Решение живого цикла order_flow (одна позиция за раз).

        Args:
            candles: свечи рабочего ТФ (старые → новые, последняя закрытая).
            position: открытая позиция или None.

        Returns:
            Instruction("enter") либо None = hold (SL/TP делает биржа).
        """
        if position is not None:
            return None  # позиция под SL/TP/гвардом — не мешаем
        # позиций нет → книги сторон чисты (в live закрытия приходят сюда)
        self._book["long"].clear()
        self._book["short"].clear()
        d = self._eval(candles)
        if d.kind != "enter":
            return None
        return Instruction(
            action="enter",
            side=d.side,
            price=d.level,
            stop=d.stop,
            take=d.tp1,
            reason=d.reason,
        )

    # ==================== бэктест ====================

    def check_signal(self, candles: list[Candle]) -> Signal:
        """Хук ``backtest.py --strategy flat``.

        Args:
            candles: свечи рабочего ТФ от старых к новым.

        Returns:
            Signal(buy/sell/hold): entry_price = уровень сетки,
            SL = граница VA ±5%, TP1 = POC (tp_split), TP2 = обратная
            сетка −1% (остаток позиции).
        """
        if not candles:
            return Signal(action="hold", reason="нет свечей")
        d = self._eval(candles)
        if d.kind != "enter":
            return Signal(action="hold", reason=d.reason)
        return Signal(
            action="buy" if d.side == "long" else "sell",
            reason=d.reason,
            stop_loss=d.stop,
            take_profit=d.tp1,
            entry_price=d.level,
            take_profit2=d.tp2,
            tp_split=self.params.tp_split,
        )

    def on_position_closed(self, direction: str) -> None:
        """Сторона опустела (SL/TP) — освободить её книгу уровней."""
        self._book[direction].clear()
