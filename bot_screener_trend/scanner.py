"""Скринер тренда по индикаторам robot_trend: скользящие средние + ADX.

Определение тренда:
  UP:   цена > EMA_FAST > EMA_SLOW > EMA_MACRO, ADX >= порога, +DI > -DI
  DOWN: цена < EMA_FAST < EMA_SLOW < EMA_MACRO, ADX >= порога, -DI > +DI

Score = сила тренда: ADX x 2 + зазор EMA_FAST/EMA_SLOW x 5 (максимум 100).
Скринер только находит сигналы: стоп/тейк и размер позиции не считает.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.indicators import adx_di, ema
from core.logger import get_logger

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))


@dataclass
class ScanConfig:
    """Параметры скринера (по умолчанию — как в robot_trend)."""

    ema_fast: int = 23
    ema_slow: int = 50
    ema_macro: int = 200
    adx_period: int = 14
    adx_threshold: float = 30.0
    min_turnover_24h: float = 10_000_000
    exclude_symbols: list[str] = field(default_factory=list)


@dataclass
class ScanResult:
    symbol: str
    timeframe: str
    price: float
    trend: str  # "UP" / "DOWN"
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_macro: float = 0.0
    spread_pct: float = 0.0  # зазор EMA_FAST / EMA_SLOW, %
    adx_value: float = 0.0
    di_plus: float = 0.0
    di_minus: float = 0.0
    score: int = 0
    turnover_24h: float = 0.0
    signal_time: str = ""


def analyze_symbol(
    symbol: str,
    candles: list[dict],
    *,
    cfg: ScanConfig | None = None,
    timeframe: str = "1",
    turnover_24h: float = 0.0,
) -> ScanResult | None:
    """Определить тренд символа по EMA + ADX/DI (логика robot_trend).

    Args:
        symbol: имя пары.
        candles: свечи [{open_time, open, high, low, close, volume}].
        cfg: конфигурация.
        timeframe: таймфрейм.
        turnover_24h: оборот за 24ч.

    Returns:
        ScanResult или None если данных недостаточно / тренда нет.
    """
    if cfg is None:
        cfg = ScanConfig()

    if len(candles) < cfg.ema_macro + 2:
        return None

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    price = closes[-1]

    fast_vals = ema(closes, cfg.ema_fast)
    slow_vals = ema(closes, cfg.ema_slow)
    macro_vals = ema(closes, cfg.ema_macro)
    adx_vals, di_plus_vals, di_minus_vals = adx_di(
        highs, lows, closes, cfg.adx_period
    )
    if any(not vals for vals in (fast_vals, slow_vals, macro_vals, adx_vals)):
        return None

    ema_f = fast_vals[-1]
    ema_s = slow_vals[-1]
    ema_m = macro_vals[-1]
    adx_val = adx_vals[-1]
    di_p = di_plus_vals[-1]
    di_m = di_minus_vals[-1]
    spread_pct = abs(ema_f - ema_s) / ema_s * 100.0 if ema_s > 0 else 0.0

    uptrend = (
        price > ema_f > ema_s > ema_m
        and adx_val >= cfg.adx_threshold
        and di_p > di_m
    )
    downtrend = (
        price < ema_f < ema_s < ema_m
        and adx_val >= cfg.adx_threshold
        and di_m > di_p
    )

    if uptrend:
        trend = "UP"
    elif downtrend:
        trend = "DOWN"
    else:
        return None

    score = int(min(100.0, adx_val * 2.0 + spread_pct * 5.0))

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        trend=trend,
        ema_fast=ema_f,
        ema_slow=ema_s,
        ema_macro=ema_m,
        spread_pct=spread_pct,
        adx_value=adx_val,
        di_plus=di_p,
        di_minus=di_m,
        score=score,
        turnover_24h=turnover_24h,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
    )
