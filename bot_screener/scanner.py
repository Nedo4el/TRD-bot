"""Логика скринера: консолидация + прорыв.

На каждой итерации:
1. Принимает свечи для одного символа.
2. Ищет узкий диапазон (N свечей в пределах X%).
3. Проверяет, выходит ли последняя свеча за этот диапазон.
4. Индикаторы — бонус к скору, не жёсткий фильтр.
5. Возвращает результат с оценкой качества 0-100.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.indicators import adx, atr, bollinger, rsi
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
    signal_time: str = ""  # время сигнала (HH:MM UTC)


def scan_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    turnover_24h: float = 0.0,
    *,
    volume_spike: float = 1.3,
    volume_drop_before: float = 0.8,
    bbw_threshold: float = 0.05,
    adx_threshold: int = 15,
    rsi_long: int = 50,
    rsi_short: int = 50,
    ema_period: int = 20,
    rsi_period: int = 14,
    adx_period: int = 14,
    atr_period: int = 14,
    bb_period: int = 20,
    volume_ma_period: int = 20,
) -> ScanResult:
    """Просканировать один символ.

    Логика: ищем узкую консолидацию (N свечей в пределах X%),
    потом проверяем, выходит ли последняя свеча за этот диапазон.

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

    consolidation_period = 10
    consolidation_range_pct = 0.008

    min_candles = (
        max(consolidation_period, bb_period, adx_period * 2, volume_ma_period) + 5
    )
    if len(candles) < min_candles:
        return empty

    # Извлекаем массивы
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    current_close = closes[-1]
    current_volume = volumes[-1]

    # --- Консолидация: N свечей до предпоследней ---
    consol_closes = closes[-consolidation_period - 1 : -1]
    if not consol_closes:
        return empty

    consol_max = max(consol_closes)
    consol_min = min(consol_closes)
    consol_mid = (consol_max + consol_min) / 2 if consol_max + consol_min > 0 else 1.0

    # Диапазон консолидации в процентах от средней цены
    consol_range = (consol_max - consol_min) / consol_mid if consol_mid > 0 else 1.0

    # Консолидация должна быть узкой
    if consol_range >= consolidation_range_pct:
        return empty

    # --- Прорыв: последняя свеча выходит за диапазон ---
    long_breakout = current_close > consol_max
    short_breakout = current_close < consol_min

    if not long_breakout and not short_breakout:
        return empty

    # --- Индикаторы (бонусы, не фильтры) ---
    rsi_vals = rsi(closes, rsi_period)
    adx_vals = adx(highs, lows, closes, adx_period)
    atr_vals = atr(highs, lows, closes, atr_period)
    upper, middle, lower = bollinger(closes, bb_period)

    current_rsi = rsi_vals[-1] if rsi_vals else 50.0
    current_adx = adx_vals[-1] if adx_vals else 0.0
    current_atr = atr_vals[-1] if atr_vals else 0.0
    current_upper = upper[-1] if upper else 0.0
    current_middle = middle[-1] if middle else 1.0
    current_lower = lower[-1] if lower else 0.0

    bbw = (
        (current_upper - current_lower) / current_middle if current_middle > 0 else 0.0
    )

    # Средний объём
    vol_window = (
        volumes[-volume_ma_period - 1 : -1]
        if len(volumes) > volume_ma_period
        else volumes[:-1]
    )
    avg_volume = sum(vol_window) / len(vol_window) if vol_window else 1.0
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0.0

    prev_volume = volumes[-2] if len(volumes) >= 2 else 0.0
    prev_volume_ratio = prev_volume / avg_volume if avg_volume > 0 else 1.0

    # --- Определяем направление ---
    if long_breakout:
        signal = "LONG"
    else:
        signal = "SHORT"

    # --- Оценка качества (0-100) ---
    score = 0

    # Прорыв из консолидации — базовый балл +30
    score += 30

    # Тесная консолидация — бонус (чем уже, тем лучше)
    if consol_range < 0.005:
        score += 20  # <0.5%
    elif consol_range < 0.01:
        score += 15  # <1%
    elif consol_range < 0.015:
        score += 10  # <1.5%

    # Сила прорыва (насколько далеко вышел за диапазон)
    breakout_dist = abs(current_close - (consol_max if long_breakout else consol_min))
    breakout_pct = breakout_dist / consol_mid if consol_mid > 0 else 0.0
    if breakout_pct > 0.02:
        score += 15  # >2% прорыв
    elif breakout_pct > 0.01:
        score += 10  # >1% прорыв
    elif breakout_pct > 0.005:
        score += 5  # >0.5% прорыв

    # Объём — подтверждение
    if volume_ratio >= 2.0:
        score += 15
    elif volume_ratio >= 1.5:
        score += 10
    elif volume_ratio >= volume_spike:
        score += 5

    # Падение объёма перед прорывом
    if prev_volume_ratio <= 0.5:
        score += 10
    elif prev_volume_ratio <= volume_drop_before:
        score += 5

    # Индикаторы — бонусы
    if bbw < 0.03:
        score += 5  # узкие полосы

    if current_adx > 25:
        score += 5  # сильный тренд
    elif current_adx > 15:
        score += 3

    if signal == "LONG" and current_rsi > 55 or signal == "SHORT" and current_rsi < 45:
        score += 5

    # Время сигнала из последней свечи
    last_candle = candles[-1]
    signal_ts = last_candle.get("open_time", 0)
    if signal_ts:
        dt = datetime.fromtimestamp(signal_ts / 1000, tz=timezone.utc)
        signal_time = dt.strftime("%H:%M")
    else:
        signal_time = ""

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
        signal_time=signal_time,
    )
