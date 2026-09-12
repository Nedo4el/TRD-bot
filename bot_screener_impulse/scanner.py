"""Скринер импульсов: тиковый объем + ширина свечи + дельта + подтверждение.

3 условия одновременно:
1. Volume > SMA(Volume, 50) * 5-7x — всплеск тикового объема
2. (High-Low)/Close > 0.15-0.30% — аномальная ширина свечи
3. |Delta| > SMA(|Delta|, 20) * 4 — дельтовый шок

+ Фильтр подтверждения: следующая свеча не перекрывает тело импульсной.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ImpulseConfig:
    """Параметры скринера."""

    volume_sma_period: int = 50
    volume_spike_multiplier: float = 5.0
    candle_width_min: float = 0.15
    candle_width_max: float = 0.30
    delta_sma_period: int = 20
    delta_spike_multiplier: float = 4.0
    confirmation_candles: int = 2


@dataclass
class ImpulseSignal:
    """Результат анализа одного символа."""

    symbol: str
    timeframe: str
    price: float
    direction: str = ""
    volume_ratio: float = 0.0
    candle_width: float = 0.0
    delta_ratio: float = 0.0
    confirmed: bool = False
    turnover_24h: float = 0.0
    signal_time: str = ""


def _sma(values: list[float], period: int) -> list[float]:
    """Простая скользящая средняя."""
    if len(values) < period:
        return []
    result = []
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        result.append(sum(window) / period)
    return result


def _estimate_delta(candle: dict) -> float:
    """Оценка дельты по свече: buy - sell.

    Если close > open — больше покупок (дельта > 0).
    Если close < open — больше продаж (дельта < 0).
    Пропорционально телу свечи.
    """
    op = float(candle["open"])
    cl = float(candle["close"])
    hi = float(candle["high"])
    lo = float(candle["low"])
    vol = float(candle["volume"])

    rng = hi - lo
    if rng <= 0:
        return 0.0

    body = cl - op
    body_ratio = body / rng  # от -1 до +1

    return vol * body_ratio


def analyze_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    cfg: ImpulseConfig | None = None,
    turnover_24h: float = 0.0,
) -> ImpulseSignal | None:
    """Анализ одного символа: поиск импульса по 3 критериям.

    Args:
        symbol: торговая пара.
        timeframe: таймфрейм.
        candles: список свечей.
        cfg: конфигурация.
        turnover_24h: оборот за 24ч.

    Returns:
        ImpulseSignal или None если нет импульса.
    """
    if cfg is None:
        cfg = ImpulseConfig()

    min_candles = max(cfg.volume_sma_period, cfg.delta_sma_period) + cfg.confirmation_candles + 5
    if len(candles) < min_candles:
        return None

    # --- Подготовка данных ---
    volumes = [float(c["volume"]) for c in candles]
    deltas = [_estimate_delta(c) for c in candles]
    opens = [float(c["open"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    # --- SMA ---
    vol_sma = _sma(volumes, cfg.volume_sma_period)
    delta_sma = _sma([abs(d) for d in deltas], cfg.delta_sma_period)

    if not vol_sma or not delta_sma:
        return None

    # --- Поиск импульса (от конца к началу) ---
    search_end = len(candles) - cfg.confirmation_candles

    for i in range(search_end - 1, max(cfg.volume_sma_period, cfg.delta_sma_period) - 1, -1):
        vol = volumes[i]
        delta = deltas[i]
        op = opens[i]
        hi = highs[i]
        lo = lows[i]
        cl = closes[i]

        if op <= 0:
            continue

        # 1. Volume Spike
        sma_vol = vol_sma[i - cfg.volume_sma_period + 1] if i >= cfg.volume_sma_period - 1 else vol_sma[-1]
        if sma_vol <= 0:
            continue
        vol_ratio = vol / sma_vol

        if vol_ratio < cfg.volume_spike_multiplier:
            continue

        # 2. Candle Width
        rng = hi - lo
        candle_width = rng / cl * 100 if cl > 0 else 0

        if candle_width < cfg.candle_width_min:
            continue

        # 3. Delta Spike
        sma_delta = delta_sma[i - cfg.delta_sma_period + 1] if i >= cfg.delta_sma_period - 1 else delta_sma[-1]
        if sma_delta <= 0:
            continue
        delta_ratio = abs(delta) / sma_delta

        if delta_ratio < cfg.delta_spike_multiplier:
            continue

        # Направление
        direction = "LONG" if cl > op else "SHORT"

        # 4. Фильтр подтверждения
        confirmed = _check_confirmation(
            candles, i, cfg.confirmation_candles, op, cl, direction
        )

        if not confirmed:
            continue

        return ImpulseSignal(
            symbol=symbol,
            timeframe=timeframe,
            price=cl,
            direction=direction,
            volume_ratio=round(vol_ratio, 1),
            candle_width=round(candle_width, 3),
            delta_ratio=round(delta_ratio, 1),
            confirmed=True,
            turnover_24h=turnover_24h,
            signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        )

    return None


def _check_confirmation(
    candles: list[dict],
    impulse_idx: int,
    num_candles: int,
    impulse_open: float,
    impulse_close: float,
    direction: str,
) -> bool:
    """Проверить подтверждение: следующие свечи не перекрывают тело импульса.

    Для LONG: low следующей свечи >= impulse_close (не упал ниже закрытия).
    Для SHORT: high следующей свечи <= impulse_close (не вырос выше закрытия).
    """
    body_top = max(impulse_open, impulse_close)
    body_bottom = min(impulse_open, impulse_close)

    for j in range(1, num_candles + 1):
        idx = impulse_idx + j
        if idx >= len(candles):
            return False

        c = candles[idx]
        c_lo = float(c["low"])
        c_hi = float(c["high"])

        if direction == "LONG":
            # Подтверждение: цена не упала ниже закрытия импульса
            if c_lo < body_bottom:
                return False
        else:
            # Подтверждение: цена не выросла выше закрытия импульса
            if c_hi > body_top:
                return False

    return True
