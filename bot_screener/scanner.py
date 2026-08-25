"""Логика скринера: определение прорывов и скоринг.

На каждой итерации:
1. Принимает свечи для одного символа.
2. Считает индикаторы (EMA, BB, RSI, ADX, ATR).
3. Проверяет критерии прорыва.
4. Возвращает результат с оценкой качества 0-100.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.indicators import adx, atr, bollinger, ema, rsi
from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScanResult:
    """Результат сканирования одного символа."""

    symbol: str
    timeframe: str
    price: float
    signal: str  # "LONG" | "SHORT" | ""
    score: int  # 0-100
    volume_ratio: float  # текущий / средний
    bbw: float  # ширина полос Боллинджера (0-1)
    atr_value: float
    adx_value: float
    rsi_value: float
    turnover_24h: float  # оборот за 24ч в USD


def scan_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    turnover_24h: float = 0.0,
    *,
    breakout_period: int = 20,
    atr_buffer: float = 0.15,
    volume_spike: float = 1.8,
    volume_drop_before: float = 0.7,
    bbw_threshold: float = 0.03,
    adx_threshold: int = 25,
    rsi_long: int = 55,
    rsi_short: int = 45,
    ema_period: int = 20,
    rsi_period: int = 14,
    adx_period: int = 14,
    atr_period: int = 14,
    bb_period: int = 20,
    volume_ma_period: int = 20,
) -> ScanResult:
    """Просканировать один символ на предмет прорыва.

    Args:
        symbol: торговая пара.
        timeframe: таймфрейм.
        candles: список свечей [{open_time, open, high, low, close, volume}].
        turnover_24h: оборот за 24ч.

    Returns:
        ScanResult с сигналом и оценкой.
    """
    empty = ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=0.0,
        signal="",
        score=0,
        volume_ratio=0.0,
        bbw=0.0,
        atr_value=0.0,
        adx_value=0.0,
        rsi_value=0.0,
        turnover_24h=turnover_24h,
    )

    if (
        len(candles)
        < max(breakout_period, bb_period, adx_period * 2, volume_ma_period) + 5
    ):
        return empty

    # Извлекаем массивы
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    current_close = closes[-1]
    current_volume = volumes[-1]

    # --- Индикаторы ---
    ema_vals = ema(closes, ema_period)
    rsi_vals = rsi(closes, rsi_period)
    adx_vals = adx(highs, lows, closes, adx_period)
    atr_vals = atr(highs, lows, closes, atr_period)
    upper, middle, lower = bollinger(closes, bb_period)

    if not ema_vals or not rsi_vals or not adx_vals or not atr_vals or not middle:
        return empty

    current_ema = ema_vals[-1]
    current_rsi = rsi_vals[-1]
    current_adx = adx_vals[-1]
    current_atr = atr_vals[-1]
    current_upper = upper[-1]
    current_middle = middle[-1]
    current_lower = lower[-1]

    # BBW = (Upper - Lower) / Middle
    bbw = (
        (current_upper - current_lower) / current_middle if current_middle > 0 else 0.0
    )

    # Средний объём за N свечей
    vol_window = (
        volumes[-volume_ma_period - 1 : -1]
        if len(volumes) > volume_ma_period
        else volumes[:-1]
    )
    avg_volume = sum(vol_window) / len(vol_window) if vol_window else 1.0
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0.0

    # Объём ПЕРЕД последней свечой
    prev_volume = volumes[-2] if len(volumes) >= 2 else 0.0
    prev_volume_ratio = prev_volume / avg_volume if avg_volume > 0 else 1.0

    # Максимум/минимум за breakout_period свечей (не включая текущую)
    lookback_highs = highs[-breakout_period - 1 : -1]
    lookback_lows = lows[-breakout_period - 1 : -1]
    max_high = max(lookback_highs) if lookback_highs else current_close
    min_low = min(lookback_lows) if lookback_lows else current_close

    # --- Критерии прорыва ---
    long_breakout = current_close > max_high + current_atr * atr_buffer
    short_breakout = current_close < min_low - current_atr * atr_buffer

    if not long_breakout and not short_breakout:
        return empty

    # --- Подтверждение объёмом ---
    if volume_ratio < volume_spike:
        return empty

    # --- Падение объёма перед импульсом ---
    if prev_volume_ratio > volume_drop_before:
        return empty

    # --- Сжатие BBW ---
    if bbw >= bbw_threshold:
        return empty

    # --- Подтверждение тренда ---
    if long_breakout:
        if current_adx < adx_threshold:
            return empty
        if current_ema > 0 and current_close < current_ema:
            return empty
        if current_rsi < rsi_long:
            return empty
        signal = "LONG"
    else:
        if current_adx < adx_threshold:
            return empty
        if current_ema > 0 and current_close > current_ema:
            return empty
        if current_rsi > rsi_short:
            return empty
        signal = "SHORT"

    # --- Оценка качества (0-100) ---
    score = 0

    # Прорыв +50
    score += 50

    # Объём 2x+ +20
    if volume_ratio >= 2.0:
        score += 20
    elif volume_ratio >= volume_spike:
        score += 10

    # Падение объёма перед +15
    if prev_volume_ratio <= 0.5:
        score += 15
    elif prev_volume_ratio <= volume_drop_before:
        score += 10

    # BBW < 2% +10
    if bbw < 0.02:
        score += 10
    elif bbw < bbw_threshold:
        score += 5

    # ADX > 30 +5
    if current_adx > 30:
        score += 5

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=current_close,
        signal=signal,
        score=min(score, 100),
        volume_ratio=volume_ratio,
        bbw=bbw,
        atr_value=current_atr,
        adx_value=current_adx,
        rsi_value=current_rsi,
        turnover_24h=turnover_24h,
    )
