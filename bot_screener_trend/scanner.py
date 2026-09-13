"""Скринер тренда по HH/HL (восходящий) и LH/LL (нисходящий).

Логика:
1. Найти swing highs и swing lows (локальные экстремумы).
2. Определить серию HH+HL → восходящий тренд или LH+LL → нисходящий.
3. EMA фильтр: EMA_fast > EMA_slow для бычьего тренда.
4. ADX фильтр: ADX > порога — тренд достаточно сильный.
5. Score = количество подряд идущих HH/HL (или LH/LL) × вес ADX.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.indicators import adx, ema, rsi
from core.logger import get_logger

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))


@dataclass
class ScanConfig:
    swing_window: int = 5
    ema_fast: int = 20
    ema_slow: int = 50
    adx_min: float = 20.0
    min_swings: int = 3
    min_turnover_24h: float = 10_000_000
    exclude_symbols: list[str] = field(default_factory=list)


@dataclass
class SwingPoint:
    index: int
    price: float
    is_high: bool  # True = swing high, False = swing low
    time: int = 0  # open_time свечи


@dataclass
class ScanResult:
    symbol: str
    timeframe: str
    price: float
    trend: str  # "UP" / "DOWN" / "NONE"
    hh_count: int = 0
    hl_count: int = 0
    lh_count: int = 0
    ll_count: int = 0
    swing_highs: list[SwingPoint] = field(default_factory=list)
    swing_lows: list[SwingPoint] = field(default_factory=list)
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    adx_value: float = 0.0
    rsi_value: float = 0.0
    score: int = 0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    turnover_24h: float = 0.0
    signal_time: str = ""


def _find_swings(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    times: list[int],
    window: int = 5,
) -> list[SwingPoint]:
    """Найти swing highs и lows.

    Swing high: high[i] — максимум среди highs[i-window:i+window+1].
    Swing low: low[i] — минимум среди lows[i-window:i+window+1].
    """
    swings: list[SwingPoint] = []
    n = len(highs)

    for i in range(window, n - window):
        left_h = highs[i - window:i]
        right_h = highs[i + 1:i + window + 1]
        if highs[i] > max(left_h) and highs[i] > max(right_h):
            swings.append(SwingPoint(
                index=i,
                price=highs[i],
                is_high=True,
                time=times[i] if i < len(times) else 0,
            ))

        left_l = lows[i - window:i]
        right_l = lows[i + 1:i + window + 1]
        if lows[i] < min(left_l) and lows[i] < min(right_l):
            swings.append(SwingPoint(index=i, price=lows[i], is_high=False,
                time=times[i] if i < len(times) else 0,
            ))

    swings.sort(key=lambda s: s.index)
    return swings


def _classify_swings(swings: list[SwingPoint]) -> tuple[int, int, int, int]:
    """Подсчитать количество HH, HL, LH, LL.

    HH: swing high выше предыдущего swing high.
    HL: swing low выше предыдущего swing low.
    LH: swing high ниже предыдущего swing high.
    LL: swing low ниже предыдущего swing low.
    """
    hh = hl = lh = ll = 0
    last_high = None
    last_low = None
    prev_high = None
    prev_low = None

    for s in swings:
        if s.is_high:
            prev_high = last_high
            last_high = s.price
            if prev_high is not None:
                if s.price > prev_high:
                    hh += 1
                elif s.price < prev_high:
                    lh += 1
        else:
            prev_low = last_low
            last_low = s.price
            if prev_low is not None:
                if s.price > prev_low:
                    hl += 1
                elif s.price < prev_low:
                    ll += 1

    return hh, hl, lh, ll


def analyze_symbol(
    symbol: str,
    candles: list[dict],
    *,
    cfg: ScanConfig | None = None,
    timeframe: str = "1",
    turnover_24h: float = 0.0,
) -> ScanResult | None:
    """Проанализировать тренд для одного символа.

    Args:
        symbol: имя пары.
        candles: свечи [{open_time, open, high, low, close, volume}].
        cfg: конфигурация.
        timeframe: таймфрейм.
        turnover_24h: оборот за 24ч.

    Returns:
        ScanResult или None если данных недостаточно.
    """
    if cfg is None:
        cfg = ScanConfig()

    if len(candles) < max(cfg.ema_slow + 10, 50):
        return None

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    times = [c["open_time"] for c in candles]
    price = closes[-1]

    # Swing points
    swings = _find_swings(highs, lows, closes, times, cfg.swing_window)
    if len(swings) < cfg.min_swings:
        return None

    hh, hl, lh, ll = _classify_swings(swings)
    swing_highs = [s for s in swings if s.is_high]
    swing_lows = [s for s in swings if not s.is_high]

    # EMA
    ema_fast_vals = ema(closes, cfg.ema_fast)
    ema_slow_vals = ema(closes, cfg.ema_slow)
    ema_f = ema_fast_vals[-1] if ema_fast_vals else 0.0
    ema_s = ema_slow_vals[-1] if ema_slow_vals else 0.0

    # ADX
    adx_vals = adx(highs, lows, closes, 14)
    adx_val = adx_vals[-1] if adx_vals else 0.0

    # RSI
    rsi_vals = rsi(closes, 14)
    rsi_val = rsi_vals[-1] if rsi_vals else 50.0

    # Определение тренда
    uptrend = hh >= cfg.min_swings and hl >= cfg.min_swings and ema_f > ema_s
    downtrend = lh >= cfg.min_swings and ll >= cfg.min_swings and ema_f < ema_s

    if uptrend:
        trend = "UP"
    elif downtrend:
        trend = "DOWN"
    else:
        trend = "NONE"

    # Score
    score = 0
    if trend == "UP":
        score = (hh + hl) * 10
        if adx_val >= cfg.adx_min:
            score += 15
        if ema_f > ema_s:
            score += 5
    elif trend == "DOWN":
        score = (lh + ll) * 10
        if adx_val >= cfg.adx_min:
            score += 15
        if ema_f < ema_s:
            score += 5

    if trend == "NONE":
        return None

    # SL/TP
    if trend == "UP" and swing_lows:
        last_low = swing_lows[-1].price
        stop_loss = last_low
        take_profit = price + (price - last_low) * 2
    elif trend == "DOWN" and swing_highs:
        last_high = swing_highs[-1].price
        stop_loss = last_high
        take_profit = price - (last_high - price) * 2
    else:
        return None

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=price,
        trend=trend,
        hh_count=hh,
        hl_count=hl,
        lh_count=lh,
        ll_count=ll,
        swing_highs=swing_highs[-5:],
        swing_lows=swing_lows[-5:],
        ema_fast=ema_f,
        ema_slow=ema_s,
        adx_value=adx_val,
        rsi_value=rsi_val,
        score=score,
        stop_loss=stop_loss,
        take_profit=take_profit,
        turnover_24h=turnover_24h,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
    )
