"""Скринер сужения диапазона для скальпинга.

Детектирует:
1. BB squeeze — ширина полос Боллинджера сжалась до минимума за N свечей.
2. ATR compression — ATR упал на 30%+ за последние 10 свечей.
3. Треугольник/вымпел/клин — линии регрессии по локальным экстремумам.
4. ADX фильтр — ADX < 20 = ложное сжатие, ADX > 25 + squeeze = сильный сигнал.

Все пороги нормированы на цену (% от цены за свечу) — универсально для BTC и DOGE.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.indicators import adx, atr, bollinger
from core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# Конфигурация паттернов (настраивай здесь)
# ============================================================


@dataclass
class PatternConfig:
    """Все настраиваемые пороги для детекции паттернов.

    Наклоны нормированы: slope / current_price * 100 (% от цены за свечу).
    """

    # --- Extrema ---
    extrema_window: int = 5  # полуха окна поиска пивотов

    # --- Наклоны (в % от цены за свечу) ---
    slope_up: float = 0.3  # > 0.3% → восходящий
    slope_down: float = -0.3  # < -0.3% → нисходящий
    slope_flat: float = 0.1  # |slope| < 0.1% → горизонтальный

    # --- Сжатие ---
    compression_ratio: float = 0.75  # текущая ширина / ширина 10 свечей назад
    triangle_lookback: int = 30  # сколько свечей анализировать

    # --- BB squeeze ---
    bb_lookback: int = 20  # окно для минимума BB

    # --- ATR ---
    atr_lookback: int = 10  # окно падения ATR
    atr_drop_threshold: float = 0.3  # ATR упал на 30%+

    # --- ADX ---
    adx_false_threshold: int = 20  # < 20 = ложное
    adx_strong_threshold: int = 25  # > 25 = сильный

    # --- Вымпел ---
    pennant_max_candles: int = 15  # макс. длина вымпела
    pennant_impulse: float = 0.03  # импульс 3% до паттерна

    # --- Индикаторы ---
    bb_period: int = 20
    bb_dev: float = 2.0
    atr_period: int = 14
    adx_period: int = 14


# дефолтный конфиг
DEFAULT_CONFIG = PatternConfig()


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
    current_price: float,
    cfg: PatternConfig,
) -> tuple[float, float, float, SqueezeType | None]:
    """Детектировать паттерн сужения через линии регрессии.

    Наклоны нормированы на цену — пороги универсальны.

    Returns:
        (norm_upper_slope, norm_lower_slope, compression_coef, squeeze_type).
    """
    lookback = cfg.triangle_lookback
    if len(highs) < lookback or len(lows) < lookback:
        return 0.0, 0.0, 0.0, None

    h = highs[-lookback:]
    l = lows[-lookback:]

    maxs_idx, mins_idx = _find_local_extrema(h, cfg.extrema_window)

    if len(maxs_idx) < 2 or len(mins_idx) < 2:
        return 0.0, 0.0, 0.0, None

    max_vals = [h[i] for i in maxs_idx[-2:]]
    min_vals = [l[i] for i in mins_idx[-2:]]

    raw_upper, _ = _linear_regression(max_vals)
    raw_lower, _ = _linear_regression(min_vals)

    # Нормализация: % от цены за свечу
    norm_upper = _normalize_slope(raw_upper, current_price)
    norm_lower = _normalize_slope(raw_lower, current_price)

    # Разница наклонов: > 0 = линии сходятся
    slope_diff = norm_upper - norm_lower

    # Сжатие: текущая высота vs 5 свечей назад
    lookback_h = min(5, len(h) - 1)
    current_height = h[-1] - l[-1]
    prev_height = (
        h[-lookback_h] - l[-lookback_h] if lookback_h < len(h) else current_height
    )

    if prev_height <= 0:
        return norm_upper, norm_lower, 0.0, None

    compression_coef = 1.0 - (current_height / prev_height)

    # Определяем тип паттерна
    squeeze_type: SqueezeType | None = None

    # Линии сходятся + есть сжатие
    if slope_diff > 0 and compression_coef > (1.0 - cfg.compression_ratio):
        avg_abs = (abs(norm_upper) + abs(norm_lower)) / 2

        if avg_abs < cfg.slope_flat:
            squeeze_type = SqueezeType.PENNANT  # горизонтальный
        elif abs(norm_upper) > cfg.slope_up and abs(norm_lower) > cfg.slope_down:
            squeeze_type = SqueezeType.WEDGE  # обе крутые
        else:
            squeeze_type = SqueezeType.TRIANGLE  # стандартный

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
    cfg: PatternConfig | None = None,
    turnover_24h: float = 0.0,
    spread_pct: float = 0.0,
) -> ScanResult:
    """Просканировать один символ на сжатие диапазона.

    Args:
        symbol: торговая пара.
        timeframe: таймфрейм.
        candles: список свечей.
        cfg: конфигурация порогов (None = дефолт).
        turnover_24h: оборот за 24ч.
        spread_pct: спред в %.

    Returns:
        ScanResult с результатом.
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG

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

    min_candles = (
        max(
            cfg.bb_period,
            cfg.atr_period * 2,
            cfg.adx_period * 2,
            cfg.triangle_lookback,
        )
        + 10
    )
    if len(candles) < min_candles:
        return empty

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    current_price = closes[-1]
    if current_price <= 0:
        return empty

    # === Индикаторы ===
    upper, middle, lower = bollinger(closes, cfg.bb_period, cfg.bb_dev)
    atr_vals = atr(highs, lows, closes, cfg.atr_period)
    adx_vals = adx(highs, lows, closes, cfg.adx_period)

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

    # === Детекция ===
    bb_squeeze, _ = _detect_bb_squeeze(bbw_history, cfg.bb_lookback)
    atr_compression, _ = _detect_atr_compression(
        atr_vals,
        cfg.atr_lookback,
        cfg.atr_drop_threshold,
    )
    norm_upper, norm_lower, compression_coef, triangle_type = _detect_triangle(
        highs,
        lows,
        current_price,
        cfg,
    )

    logger.debug(
        "%s %s: upper=%.3f%% lower=%.3f%% comp=%.3f ADX=%.1f BB%=%.2f",
        symbol,
        timeframe,
        norm_upper,
        norm_lower,
        compression_coef,
        current_adx,
        bbw_pct,
    )

    # === ADX фильтр ===
    if current_adx < cfg.adx_false_threshold:
        return ScanResult(
            symbol=symbol,
            timeframe=timeframe,
            price=current_price,
            squeeze_type=None,
            atr_current=current_atr,
            atr_percent=atr_pct,
            bb_width=bbw_pct,
            adx_value=current_adx,
            direction="",
            stop_loss=0.0,
            take_profit=0.0,
            score=0,
            slope_upper=norm_upper,
            slope_lower=norm_lower,
            compression_coef=compression_coef,
            turnover_24h=turnover_24h,
            spread_pct=spread_pct,
        )

    # === Тип сжатия ===
    squeeze_type: SqueezeType | None = None
    if triangle_type is not None:
        squeeze_type = triangle_type
    elif bb_squeeze or atr_compression:
        squeeze_type = SqueezeType.BB_SQUEEZE

    if squeeze_type is None:
        return ScanResult(
            symbol=symbol,
            timeframe=timeframe,
            price=current_price,
            squeeze_type=None,
            atr_current=current_atr,
            atr_percent=atr_pct,
            bb_width=bbw_pct,
            adx_value=current_adx,
            direction="",
            stop_loss=0.0,
            take_profit=0.0,
            score=0,
            slope_upper=norm_upper,
            slope_lower=norm_lower,
            compression_coef=compression_coef,
            turnover_24h=turnover_24h,
            spread_pct=spread_pct,
        )

    # === Направление ===
    last_close = closes[-1]
    candle_range = highs[-1] - lows[-1]

    if candle_range > 0:
        body_pos = (last_close - lows[-1]) / candle_range
        direction = "LONG" if body_pos > 0.6 else "SHORT" if body_pos < 0.4 else ""
    else:
        direction = ""

    if not direction:
        direction = "LONG" if last_close > middle[-1] else "SHORT"

    # === SL / TP ===
    stop_loss = current_atr * 1.5
    take_profit = candle_range if candle_range > 0 else current_atr

    # === Score ===
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
    if current_adx > cfg.adx_strong_threshold:
        score += 10
    elif current_adx > 20:
        score += 5
    if spread_pct > 0.05:
        score = max(0, score - 20)

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=current_price,
        squeeze_type=squeeze_type,
        atr_current=current_atr,
        atr_percent=atr_pct,
        bb_width=bbw_pct,
        adx_value=current_adx,
        direction=direction,
        stop_loss=stop_loss,
        take_profit=take_profit,
        score=min(score, 100),
        slope_upper=norm_upper,
        slope_lower=norm_lower,
        compression_coef=compression_coef,
        turnover_24h=turnover_24h,
        spread_pct=spread_pct,
    )


# ============================================================
# Backtest: подбор параметров
# ============================================================


def backtest_params(
    candles: list[dict],
    symbol: str = "TEST",
    timeframe: str = "5m",
) -> dict[str, dict]:
    """Прогнать скринер с разными параметрами и показать статистику.

    Используй для подбора оптимальных extrema_window / compression_ratio.

    Args:
        candles: история свечей (минимум 100).
        symbol: имя для логов.
        timeframe: TF для логов.

    Returns:
        {param_name: {signals: int, avg_score: float, types: {...}}}.
    """
    results: dict[str, dict] = {}

    configs = {
        "w3_comp75": PatternConfig(extrema_window=3, compression_ratio=0.75),
        "w5_comp75": PatternConfig(extrema_window=5, compression_ratio=0.75),
        "w7_comp75": PatternConfig(extrema_window=7, compression_ratio=0.75),
        "w10_comp75": PatternConfig(extrema_window=10, compression_ratio=0.75),
        "w5_comp60": PatternConfig(extrema_window=5, compression_ratio=0.60),
        "w5_comp85": PatternConfig(extrema_window=5, compression_ratio=0.85),
        "w5_slope02": PatternConfig(extrema_window=5, slope_up=0.2, slope_down=-0.2),
        "w5_slope04": PatternConfig(extrema_window=5, slope_up=0.4, slope_down=-0.4),
    }

    for name, cfg in configs.items():
        signals = 0
        total_score = 0
        type_counts: dict[str, int] = {}

        # Сканируем скользящим окном
        window_size = cfg.triangle_lookback + 10
        for i in range(window_size, len(candles)):
            chunk = candles[i - window_size : i + 10]
            result = scan_symbol(symbol, timeframe, chunk, cfg=cfg)

            if result.squeeze_type:
                signals += 1
                total_score += result.score
                t = result.squeeze_type.value
                type_counts[t] = type_counts.get(t, 0) + 1

        avg_score = total_score / signals if signals > 0 else 0.0
        results[name] = {
            "signals": signals,
            "avg_score": round(avg_score, 1),
            "types": type_counts,
        }

    return results
