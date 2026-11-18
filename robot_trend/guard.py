"""Защита открытой позиции: CVD-дивергенция, absorption, OI-дивергенция.

Чистая логика без сети: сюда передают закрытые свечи, ряд CVD и точки OI,
модуль возвращает вердикт (HOLD / WARN / EXIT_PARTIAL / EXIT_FULL).
Исполнение ордеров и логирование — в ``order_flow.py``.
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field

from core.bybit_client import Candle
from core.indicators import atr, sma
from robot_trend.config import _env_bool, _env_float, _env_int


class GuardAction(enum.Enum):
    """Сила сигнала защиты (больше значение — важнее действие)."""

    HOLD = 0
    WARN = 1
    EXIT_PARTIAL = 2
    EXIT_FULL = 3


@dataclass(frozen=True)
class GuardParams:
    """Параметры защиты из окружения (``.env`` бота)."""

    enabled: bool = True
    dry_run: bool = True
    cvd_enabled: bool = True
    cvd_window: int = 30
    cvd_exit_mode: str = "partial"
    partial_pct: float = 0.5
    partial_mode: str = "both"
    partial_min_profit: float = 0.05
    partial_fallback_pct: float = 0.12
    partial_signal: str = "cvd+oi"
    absorption_enabled: bool = True
    absorption_vol_mult: float = 2.0
    absorption_body_max: float = 0.3
    oi_enabled: bool = True
    oi_window: int = 30
    oi_drop_pct: float = 0.03
    funding_high: float = 0.0005
    min_volume_usd: float = 10_000.0

    @classmethod
    def from_env(cls) -> GuardParams:
        """Собрать параметры из окружения."""
        return cls(
            enabled=_env_bool("GUARD_ENABLED", True),
            dry_run=_env_bool("GUARD_DRY_RUN", True),
            cvd_enabled=_env_bool("CVD_ENABLED", True),
            cvd_window=_env_int("CVD_WINDOW", 30),
            cvd_exit_mode=os.getenv("CVD_EXIT_MODE", "partial").strip().lower(),
            partial_pct=_env_float("CVD_PARTIAL_PCT", 0.5),
            partial_mode=os.getenv("PARTIAL_MODE", "both").strip().lower(),
            partial_min_profit=_env_float("PARTIAL_MIN_PROFIT", 0.05),
            partial_fallback_pct=_env_float("PARTIAL_FALLBACK_PCT", 0.12),
            partial_signal=os.getenv("PARTIAL_SIGNAL", "cvd+oi").strip().lower(),
            absorption_enabled=_env_bool("ABSORPTION_ENABLED", True),
            absorption_vol_mult=_env_float("ABSORPTION_VOL_MULT", 2.0),
            absorption_body_max=_env_float("ABSORPTION_BODY_MAX", 0.3),
            oi_enabled=_env_bool("OI_ENABLED", True),
            oi_window=_env_int("OI_WINDOW", 30),
            oi_drop_pct=_env_float("OI_DROP_PCT", 0.03),
            funding_high=_env_float("FUNDING_HIGH_THRESHOLD", 0.0005),
            min_volume_usd=_env_float("MIN_VOLUME_USD", 10_000.0),
        )

    def __post_init__(self) -> None:
        if self.cvd_exit_mode not in ("partial", "full"):
            raise ValueError("CVD_EXIT_MODE должен быть partial или full")
        if self.cvd_window < 3 or self.oi_window < 3:
            raise ValueError("CVD_WINDOW и OI_WINDOW должны быть >= 3")
        if not 0 < self.partial_pct < 1:
            raise ValueError("CVD_PARTIAL_PCT должен быть в (0, 1)")
        if self.partial_mode not in ("level", "signal", "both"):
            raise ValueError("PARTIAL_MODE должен быть level, signal или both")
        if self.partial_signal not in ("cvd+oi", "cvd", "oi", "any"):
            raise ValueError("PARTIAL_SIGNAL должен быть cvd+oi, cvd, oi или any")
        if not 0 <= self.partial_min_profit < 1:
            raise ValueError("PARTIAL_MIN_PROFIT должен быть в [0, 1)")
        if not 0 <= self.partial_fallback_pct < 1:
            raise ValueError("PARTIAL_FALLBACK_PCT должен быть в [0, 1)")
        if not 0 <= self.oi_drop_pct < 1:
            raise ValueError("OI_DROP_PCT должен быть в [0, 1)")
        if self.absorption_vol_mult <= 0 or self.absorption_body_max <= 0:
            raise ValueError("ABSORPTION_VOL_MULT и ABSORPTION_BODY_MAX должны быть > 0")
        if self.funding_high < 0 or self.min_volume_usd < 0:
            raise ValueError("FUNDING_HIGH_THRESHOLD и MIN_VOLUME_USD не могут быть < 0")


@dataclass(frozen=True)
class OiPoint:
    """Снимок открытого интереса в момент времени."""

    ts_ms: int
    oi: float
    price: float = 0.0


@dataclass(frozen=True)
class GuardVerdict:
    """Решение одного модуля защиты."""

    action: GuardAction
    module: str
    reason: str
    details: dict[str, float] = field(default_factory=dict)


HOLD = GuardVerdict(GuardAction.HOLD, "", "hold")


def partial_qty(qty: float, pct: float, step: float) -> float:
    """Объём частичного закрытия, кратный шагу биржи (вниз)."""
    if qty <= 0 or pct <= 0 or step <= 0:
        return 0.0
    raw = qty * pct
    return float(int(raw / step + 1e-9) * step)


def _notional(candle: Candle) -> float:
    """Оборот свечи в USDT (объём в базовом активе × цена)."""
    return candle.volume * candle.close


class PositionGuard:
    """Три независимых проверки ослабления движения в сторону позиции."""

    def __init__(self, params: GuardParams | None = None) -> None:
        self.params = params if params is not None else GuardParams()

    # ==================== модуль 1: CVD ====================

    def check_cvd_divergence(
        self,
        candles: list[Candle],
        cvd: list[float],
        side: str,
    ) -> GuardVerdict:
        """Дивергенция цены и накопленной дельты объёма (CVD)."""
        p = self.params
        if not p.enabled or not p.cvd_enabled:
            return GuardVerdict(GuardAction.HOLD, "cvd", "disabled")
        if not self._enough(candles, cvd, p.cvd_window):
            return GuardVerdict(GuardAction.HOLD, "cvd", "history")
        if _notional(candles[-1]) < p.min_volume_usd:
            return GuardVerdict(GuardAction.HOLD, "cvd", "low_volume")

        wc = candles[-p.cvd_window :]
        wv = cvd[-p.cvd_window :]
        last = candles[-1]
        if side == "long":
            idx = max(range(len(wc) - 1), key=lambda i: wc[i].high)
            extreme_price = wc[idx].high
            extreme_cvd = wv[idx]
            diverged = last.close >= extreme_price and cvd[-1] < extreme_cvd
            reason = "bearish_divergence"
        else:
            idx = min(range(len(wc) - 1), key=lambda i: wc[i].low)
            extreme_price = wc[idx].low
            extreme_cvd = wv[idx]
            diverged = last.close <= extreme_price and cvd[-1] > extreme_cvd
            reason = "bullish_divergence"

        if not diverged:
            return GuardVerdict(
                GuardAction.HOLD,
                "cvd",
                "no_divergence",
                {"price_extreme": extreme_price, "cvd_extreme": extreme_cvd},
            )
        action = (
            GuardAction.EXIT_FULL
            if p.cvd_exit_mode == "full"
            else GuardAction.EXIT_PARTIAL
        )
        return GuardVerdict(
            action,
            "cvd",
            reason,
            {
                "price": last.close,
                "price_extreme": extreme_price,
                "cvd_now": cvd[-1],
                "cvd_extreme": extreme_cvd,
            },
        )

    # ==================== модуль 2: absorption ====================

    def check_absorption(self, candles: list[Candle], side: str) -> GuardVerdict:
        """Крупный объём без движения тела — покупатели/продавцы поглощены."""
        p = self.params
        if not p.enabled or not p.absorption_enabled:
            return GuardVerdict(GuardAction.HOLD, "absorption", "disabled")
        need = 20 + 15
        if len(candles) < need:
            return GuardVerdict(GuardAction.HOLD, "absorption", "history")
        last = candles[-1]
        if _notional(last) < p.min_volume_usd:
            return GuardVerdict(GuardAction.HOLD, "absorption", "low_volume")

        volumes = [c.volume for c in candles[-20:]]
        vol_ma = sma(volumes, 20)[-1]
        if vol_ma <= 0:
            return GuardVerdict(GuardAction.HOLD, "absorption", "no_volume")
        vol_ratio = last.volume / vol_ma

        tail = candles[-need:]
        atr_val = atr(
            [c.high for c in tail],
            [c.low for c in tail],
            [c.close for c in tail],
            period=14,
        )[-1]
        body = abs(last.close - last.open)
        body_ratio = body / atr_val if atr_val > 0 else float("inf")

        details = {"volume_ratio": vol_ratio, "body_ratio": body_ratio}
        if vol_ratio < p.absorption_vol_mult or body_ratio > p.absorption_body_max:
            return GuardVerdict(GuardAction.HOLD, "absorption", "no_absorption", details)

        prev = candles[-2]
        exhausted = last.close < prev.low if side == "long" else last.close > prev.high
        return GuardVerdict(
            GuardAction.EXIT_PARTIAL if exhausted else GuardAction.WARN,
            "absorption",
            "absorption_exhausted" if exhausted else "absorption_warning",
            details,
        )

    # ==================== модуль 3: OI ====================

    def check_oi_divergence(
        self,
        candles: list[Candle],
        oi_points: list[OiPoint],
        funding: float,
        side: str,
    ) -> GuardVerdict:
        """Цена обновила экстремум, а открытый интерес — нет: денег не добавилось."""
        p = self.params
        if not p.enabled or not p.oi_enabled:
            return GuardVerdict(GuardAction.HOLD, "oi", "disabled")
        if len(candles) < p.oi_window or not oi_points:
            return GuardVerdict(GuardAction.HOLD, "oi", "history")
        if _notional(candles[-1]) < p.min_volume_usd:
            return GuardVerdict(GuardAction.HOLD, "oi", "low_volume")

        w = candles[-p.oi_window :]
        if side == "long":
            idx = max(range(len(w)), key=lambda i: w[i].close)
            price_extreme = w[idx].close
            diverged = candles[-1].close >= price_extreme
            reason = "oi_no_new_high"
        else:
            idx = min(range(len(w)), key=lambda i: w[i].close)
            price_extreme = w[idx].close
            diverged = candles[-1].close <= price_extreme
            reason = "oi_no_new_low"

        close_ms = w[idx].open_time + 5 * 60_000
        at_extreme = [pt for pt in oi_points if pt.ts_ms <= close_ms]
        if not at_extreme:
            return GuardVerdict(GuardAction.HOLD, "oi", "history")
        oi_extreme = at_extreme[-1].oi
        oi_now = oi_points[-1].oi
        dropping = oi_now < oi_extreme * (1.0 - p.oi_drop_pct)

        details = {
            "price": candles[-1].close,
            "price_extreme": price_extreme,
            "oi_now": oi_now,
            "oi_extreme": oi_extreme,
            "funding": funding,
        }
        if not diverged or not dropping:
            return GuardVerdict(GuardAction.HOLD, "oi", "no_divergence", details)

        # рост фандинга при падающем OI — позиции перегружены, выходим целиком
        crowded = funding >= p.funding_high and oi_now < oi_extreme
        return GuardVerdict(
            GuardAction.EXIT_FULL if crowded else GuardAction.EXIT_PARTIAL,
            "oi",
            "oi_crowded" if crowded else reason,
            details,
        )

    # ==================== общий вердикт ====================

    def evaluate(
        self,
        candles: list[Candle],
        cvd: list[float],
        oi_points: list[OiPoint],
        funding: float,
        side: str,
    ) -> GuardVerdict:
        """Самый сильный вердикт из трёх модулей (CVD при равной силе)."""
        verdicts = [
            self.check_cvd_divergence(candles, cvd, side),
            self.check_absorption(candles, side),
            self.check_oi_divergence(candles, oi_points, funding, side),
        ]
        best = verdicts[0]
        for verdict in verdicts[1:]:
            if verdict.action.value > best.action.value:
                best = verdict
        return best

    @staticmethod
    def _enough(candles: list[Candle], cvd: list[float], window: int) -> bool:
        return len(candles) >= window and len(cvd) == len(candles)
