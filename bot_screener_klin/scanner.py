"""Скринер сужения диапазона для скальпинга.

Детектирует:
1. BB squeeze — ширина полос Боллинджера сжалась до минимума за N свечей.
2. ATR compression — ATR упал на 30%+ за последние 10 свечей.
3. Треугольник/вымпел/клин — линии регрессии по локальным экстремумам.
4. ADX фильтр — ADX < 20 = ложное сжатие, ADX > 25 + squeeze = сильный сигнал.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
from enum import Enum

from core.indicators import adx, atr, bollinger
from core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# Паттерны
# ============================================================


class SqueezeType(Enum):
    """Тип сужения."""

    BB_SQUEEZE = "Сжатие BB"
    TRIANGLE = "Треугольник"
    PENNANT = "Вымпел"
    WEDGE = "Клин"


@dataclass
class ScanResult:
    """Результат сканирования одного символа."""

    symbol: str
    timeframe: str
    price: float
    squeeze_type: SqueezeType | None
    atr_current: float
    atr_percent: float
    bb_width: float
    adx_value: float
    direction: str
    stop_loss: float
    take_profit: float
    score: int
    slope_upper: float = 0.0
    slope_lower: float = 0.0
    compression_coef: float = 0.0
    spread_pct: float = 0.0
    turnover_24h: float = 0.0
    signal_time: str = ""


# ============================================================
# Вспомогательные функции
# ============================================================


def _linear_regression(values: list[float]) -> tuple[float, float]:
    """Простая линейная регрессия: y = slope * x + intercept."""
    n = len(values)
    if n < 2:
        return 0.0, 0.0

    sum_x = n * (n - 1) / 2
    sum_y = sum(values)
    sum_xy = sum(i * v for i, v in enumerate(values))
    sum_x2 = n * (n - 1) * (2 * n - 1) / 6

    denom = n * sum_x2 - sum_x * sum_x
    if abs(denom) < 1e-12:
        return 0.0, sum_y / n

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _normalize_slope(slope: float, price: float) -> float:
    """Нормализовать наклон: slope / price * 100 (% от цены за свечу).

    Пример:
        BTC: slope=500, price=100000 → 0.5%
        DOGE: slope=0.005, price=0.1 → 0.5%
    """
    if price <= 0:
        return 0.0
    return (slope / price) * 100


def _find_local_extrema(
    values: list[float],
    window: int = 5,
) -> tuple[list[int], list[int]]:
    """Найти локальные максимумы и минимумы."""
    n = len(values)
    maxs: list[int] = []
    mins: list[int] = []

    for i in range(window, n - window):
        is_max = True
        is_min = True
        for j in range(i - window, i + window + 1):
            if j == i:
                continue
            if values[j] >= values[i]:
                is_max = False
            if values[j] <= values[i]:
                is_min = False
        if is_max:
            maxs.append(i)
        if is_min:
            mins.append(i)

    return maxs, mins


def _detect_triangle(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    current_price: float,
) -> tuple[float, float, float, SqueezeType | None]:
    """Детектировать клин/треугольник через регрессию по экстремумам."""
    extrema_window = 5
    triangle_lookback = 30
    slope_flat = 0.1

    lookback = triangle_lookback
    if len(highs) < lookback or len(lows) < lookback:
        return 0.0, 0.0, 0.0, None

    h = highs[-lookback:]
    lo = lows[-lookback:]
    c = closes[-lookback:] if len(closes) >= lookback else closes

    if len(c) >= 10:
        up_count = sum(1 for i in range(1, len(c)) if c[i] > c[i - 1])
        down_count = sum(1 for i in range(1, len(c)) if c[i] < c[i - 1])
        total = len(c) - 1
        trend_ratio = max(up_count, down_count) / total if total > 0 else 0
        if trend_ratio > 0.80:
            return 0.0, 0.0, 0.0, None

    maxs_idx, mins_idx = _find_local_extrema(h, extrema_window)

    if len(maxs_idx) < 2 or len(mins_idx) < 2:
        return 0.0, 0.0, 0.0, None

    max_vals = [h[i] for i in maxs_idx]
    max_slope, max_intercept = _linear_regression(max_vals)
    max_positions = list(range(len(max_vals)))
    max_vals_fit = [max_slope * x + max_intercept for x in max_positions]

    avg_price = sum(max_vals) / len(max_vals) if max_vals else current_price
    norm_upper = (max_slope / avg_price * 100) if avg_price > 0 else 0.0

    min_vals = [lo[i] for i in mins_idx]
    min_slope, min_intercept = _linear_regression(min_vals)
    min_positions = list(range(len(min_vals)))
    min_vals_fit = [min_slope * x + min_intercept for x in min_positions]

    avg_price_l = sum(min_vals) / len(min_vals) if min_vals else current_price
    norm_lower = (min_slope / avg_price_l * 100) if avg_price_l > 0 else 0.0

    highs_decreasing = max_slope < 0
    lows_increasing = min_slope > 0

    if not highs_decreasing and not lows_increasing:
        return 0.0, 0.0, 0.0, None

    first_upper = max_vals_fit[0]
    first_lower = min_vals_fit[0]
    last_upper = max_vals_fit[-1]
    last_lower = min_vals_fit[-1]

    first_width = first_upper - first_lower
    last_width = last_upper - last_lower

    if first_width <= 0:
        return 0.0, 0.0, 0.0, None

    compression_coef = 1.0 - (last_width / first_width) if first_width > 0 else 0.0

    if compression_coef < 0.15:
        return 0.0, 0.0, 0.0, None

    slope_diff = max_slope - min_slope
    if abs(slope_diff) < 1e-12:
        return 0.0, 0.0, 0.0, None

    apex_x = (min_intercept - max_intercept) / slope_diff

    if apex_x < 3 or apex_x > 30:
        return 0.0, 0.0, 0.0, None

    squeeze_type: SqueezeType | None = None
    if highs_decreasing and lows_increasing:
        avg_abs = (abs(norm_upper) + abs(norm_lower)) / 2
        if avg_abs < slope_flat:
            squeeze_type = SqueezeType.PENNANT
        else:
            squeeze_type = SqueezeType.TRIANGLE
    elif highs_decreasing:
        squeeze_type = SqueezeType.WEDGE
    elif lows_increasing:
        squeeze_type = SqueezeType.WEDGE

    return norm_upper, norm_lower, compression_coef, squeeze_type


def _detect_bb_squeeze(
    bbw_values: list[float],
    lookback: int = 20,
) -> tuple[bool, float]:
    """Детектировать сжатие полос Боллинджера."""
    if len(bbw_values) < lookback:
        return False, 0.0

    window = bbw_values[-lookback:]
    current = bbw_values[-1]
    min_bbw = min(window)

    is_squeeze = current <= min_bbw * 1.1 if min_bbw > 0 else False
    return is_squeeze, current


def _detect_atr_compression(
    atr_values: list[float],
    lookback: int = 10,
    drop_threshold: float = 0.3,
) -> tuple[bool, float]:
    """Детектировать сжатие ATR."""
    if len(atr_values) < lookback:
        return False, 0.0

    current = atr_values[-1]
    prev = atr_values[-lookback]

    if prev <= 0:
        return False, current

    drop_pct = 1.0 - (current / prev)
    return drop_pct >= drop_threshold, current


# ============================================================
# Основная функция сканирования
# ============================================================


def scan_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    turnover_24h: float = 0.0,
    spread_pct: float = 0.0,
) -> ScanResult:
    """Просканировать один символ на сжатие диапазона."""
    bb_period = 20
    bb_dev = 2.0
    atr_period = 14
    adx_period = 14
    bb_lookback = 20
    atr_lookback = 10
    atr_drop_threshold = 0.3
    adx_false_threshold = 20
    adx_strong_threshold = 25

    empty = ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=0.0,
        squeeze_type=None,
        atr_current=0.0,
        atr_percent=0.0,
        bb_width=0.0,
        adx_value=0.0,
        direction="",
        stop_loss=0.0,
        take_profit=0.0,
        score=0,
        turnover_24h=turnover_24h,
        spread_pct=spread_pct,
    )

    min_candles = max(bb_period, atr_period * 2, adx_period * 2, 30) + 10
    if len(candles) < min_candles:
        return empty

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    current_price = closes[-1]
    if current_price <= 0:
        return empty

    upper, middle, lower = bollinger(closes, bb_period, bb_dev)
    atr_vals = atr(highs, lows, closes, atr_period)
    adx_vals = adx(highs, lows, closes, adx_period)

    if not middle or not atr_vals or not adx_vals:
        return empty

    bbw = (upper[-1] - lower[-1]) / middle[-1] if middle[-1] > 0 else 0.0
    bbw_pct = bbw * 100

    bbw_history = [
        (upper[i] - lower[i]) / middle[i] if middle[i] > 0 else 0.0
        for i in range(len(middle))
    ]

    current_atr = atr_vals[-1]
    atr_pct = (current_atr / current_price * 100) if current_price > 0 else 0.0
    current_adx = adx_vals[-1]

    bb_squeeze, _ = _detect_bb_squeeze(bbw_history, bb_lookback)
    atr_compression, _ = _detect_atr_compression(
        atr_vals, atr_lookback, atr_drop_threshold,
    )
    norm_upper, norm_lower, compression_coef, triangle_type = _detect_triangle(
        highs, lows, closes, current_price,
    )

    logger.debug(
        "%s %s: upper=%.3f%% lower=%.3f%% comp=%.3f ADX=%.1f BB%=%.2f",
        symbol, timeframe, norm_upper, norm_lower, compression_coef,
        current_adx, bbw_pct,
    )

    if current_adx < adx_false_threshold:
        return ScanResult(
            symbol=symbol, timeframe=timeframe, price=current_price,
            squeeze_type=None, atr_current=current_atr, atr_percent=atr_pct,
            bb_width=bbw_pct, adx_value=current_adx, direction="",
            stop_loss=0.0, take_profit=0.0, score=0,
            slope_upper=norm_upper, slope_lower=norm_lower,
            compression_coef=compression_coef, turnover_24h=turnover_24h,
            spread_pct=spread_pct,
            signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        )

    squeeze_type: SqueezeType | None = None
    if triangle_type is not None:
        squeeze_type = triangle_type
    elif bb_squeeze or atr_compression:
        squeeze_type = SqueezeType.BB_SQUEEZE

    if squeeze_type is None:
        return ScanResult(
            symbol=symbol, timeframe=timeframe, price=current_price,
            squeeze_type=None, atr_current=current_atr, atr_percent=atr_pct,
            bb_width=bbw_pct, adx_value=current_adx, direction="",
            stop_loss=0.0, take_profit=0.0, score=0,
            slope_upper=norm_upper, slope_lower=norm_lower,
            compression_coef=compression_coef, turnover_24h=turnover_24h,
            spread_pct=spread_pct,
            signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        )

    last_close = closes[-1]
    candle_range = highs[-1] - lows[-1]

    if candle_range > 0:
        body_pos = (last_close - lows[-1]) / candle_range
        direction = "LONG" if body_pos > 0.6 else "SHORT" if body_pos < 0.4 else ""
    else:
        direction = ""

    if not direction:
        direction = "LONG" if last_close > middle[-1] else "SHORT"

    stop_loss = current_atr * 1.5
    take_profit = candle_range if candle_range > 0 else current_atr

    score = 0
    if squeeze_type == SqueezeType.TRIANGLE:
        score += 30
    elif squeeze_type in (SqueezeType.PENNANT, SqueezeType.WEDGE):
        score += 25
    elif squeeze_type == SqueezeType.BB_SQUEEZE:
        score += 20

    if bb_squeeze:
        score += 15
    if atr_compression:
        score += 15
    if compression_coef > 0.5:
        score += 15
    elif compression_coef > 0.3:
        score += 10
    if current_adx > adx_strong_threshold:
        score += 10
    elif current_adx > 20:
        score += 5
    if spread_pct > 0.05:
        score = max(0, score - 20)

    return ScanResult(
        symbol=symbol, timeframe=timeframe, price=current_price,
        squeeze_type=squeeze_type, atr_current=current_atr, atr_percent=atr_pct,
        bb_width=bbw_pct, adx_value=current_adx, direction=direction,
        stop_loss=stop_loss, take_profit=take_profit, score=min(score, 100),
        slope_upper=norm_upper, slope_lower=norm_lower,
        compression_coef=compression_coef, turnover_24h=turnover_24h,
        spread_pct=spread_pct,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
    )


