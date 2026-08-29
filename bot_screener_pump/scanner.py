"""Accumulation screener: поиск накопления крупных игроков.

Многофакторная система из 8 независимых факторов с весами.
Каждый фактор нормирован 0..1, итоговая вероятность — взвешенная сумма.

Факторы:
  1. Ценовой диапазон (консолидация)          W=15%
  2. Аномальный объем                         W=20%
  3. Дивергенция OBV                          W=15%
  4. Сжатие Bollinger Bands                   W=10%
  5. Smart Money (Buy/Sell Ratio + крупные)   W=15%
  6. Отток с бирж (Volume / Price heuristics) W=10%
  7. RSI (нейтральная зона + дивергенция)      W=5%
  8. Ликвидность (спред + объем сделок)        W=10%
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from core.indicators import bollinger, rsi
from core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# Конфигурация
# ============================================================


@dataclass
class AccumulationConfig:
    """Все настраиваемые параметры скринера."""

    # --- Периоды ---
    analysis_period: int = 60
    volume_lookback: int = 20
    obv_divergence_lookback: int = 30
    bb_period: int = 20
    bb_dev: float = 2.0
    rsi_period: int = 14
    smart_money_lookback: int = 7
    large_trade_lookback: int = 7

    # --- Пороги ---
    range_max: float = 0.25
    range_min: float = 0.05
    volume_spike: float = 2.0
    bb_compression: float = 0.7
    min_volume_usd: float = 1_000_000
    min_trades: int = 10_000
    max_spread: float = 0.003
    min_days_listed: int = 90
    max_price: float = 1.0
    large_trade_threshold: float = 0.6

    # --- Фильтры шума ---
    max_volatility_3d: float = 0.10
    max_drop_7d: float = 0.30
    min_pump_probability: float = 0.45

    # --- Веса факторов (сумма = 1.0) ---
    weight_range: float = 0.15
    weight_volume: float = 0.20
    weight_obv: float = 0.15
    weight_bb: float = 0.10
    weight_smart_money: float = 0.15
    weight_outflow: float = 0.10
    weight_rsi: float = 0.05
    weight_liquidity: float = 0.10


DEFAULT_CONFIG = AccumulationConfig()


# ============================================================
# Модель результата
# ============================================================


@dataclass
class AccumulationSignal:
    """Результат анализа одного символа."""

    symbol: str
    timeframe: str
    price: float
    range_pct: float = 0.0
    range_position: float = 0.0
    avg_volume: float = 0.0
    buy_sell_ratio: float = 0.0

    f_range: float = 0.0
    f_volume: float = 0.0
    f_obv: float = 0.0
    f_bb: float = 0.0
    f_smart_money: float = 0.0
    f_outflow: float = 0.0
    f_rsi: float = 0.0
    f_liquidity: float = 0.0

    pump_probability: float = 0.0
    status: str = ""
    spread_pct: float = 0.0
    trades_24h: int = 0
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


def _ema(values: list[float], period: int) -> list[float]:
    """Экспоненциальная скользящая средняя."""
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Ограничить значение диапазоном [lo, hi]."""
    return max(lo, min(hi, value))


# ============================================================
# Фактор 1: Ценовой диапазон (консолидация)
# ============================================================


def _factor_range(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    cfg: AccumulationConfig,
) -> tuple[float, float, float]:
    """Консолидация в узком коридоре + текущая цена в нижней трети.

    Returns:
        (score, range_pct, range_position) — score 0..1,
        range_pct — ширина диапазона в %,
        range_position — позиция цены в диапазоне 0..1.
    """
    if len(highs) < 10:
        return 0.0, 0.0, 0.5

    period_high = max(highs)
    period_low = min(lows)

    if period_low <= 0:
        return 0.0, 0.0, 0.5

    range_pct = (period_high - period_low) / period_low

    if range_pct < cfg.range_min or range_pct > cfg.range_max:
        return 0.0, range_pct, 0.5

    current = closes[-1]
    range_position = (current - period_low) / (period_high - period_low)

    # Ширина: чем уже, тем лучше (макс. балл при range_pct ~ 5%)
    width_score = _clamp(
        1.0 - (range_pct - cfg.range_min) / (cfg.range_max - cfg.range_min)
    )

    # Позиция: чем ниже в диапазоне, тем лучше для накопления
    position_score = _clamp(1.0 - range_position)

    score = width_score * 0.5 + position_score * 0.5
    return score, range_pct, range_position


# ============================================================
# Фактор 2: Аномальный объем
# ============================================================


def _factor_volume(
    volumes: list[float],
    highs: list[float],
    lows: list[float],
    cfg: AccumulationConfig,
) -> tuple[float, float]:
    """Объем近期 вырос при стабильной цене.

    Returns:
        (score, avg_volume) — score 0..1.
    """
    n = len(volumes)
    lookback = min(cfg.volume_lookback, n // 3)
    if lookback < 5:
        return 0.0, 0.0

    recent = volumes[-lookback:]
    prev = (
        volumes[-lookback * 3 : -lookback] if n >= lookback * 3 else volumes[:-lookback]
    )

    avg_recent = statistics.mean(recent)
    avg_prev = statistics.mean(prev) if prev else avg_recent

    if avg_prev <= 0:
        return 0.0, avg_recent

    volume_ratio = avg_recent / avg_prev

    # Проверяем, что цена не вышла за диапазон
    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]
    prev_highs = (
        highs[-lookback * 3 : -lookback] if n >= lookback * 3 else highs[:-lookback]
    )
    prev_lows = (
        lows[-lookback * 3 : -lookback] if n >= lookback * 3 else lows[:-lookback]
    )

    price_range_recent = (
        (max(recent_highs) - min(recent_lows)) / min(recent_lows)
        if min(recent_lows) > 0
        else 0
    )
    price_range_prev = (
        (max(prev_highs) - min(prev_lows)) / min(prev_lows)
        if prev and min(prev_lows) > 0
        else price_range_recent
    )

    price_stable = price_range_recent <= price_range_prev * 1.3

    if volume_ratio < cfg.volume_spike:
        return 0.0, avg_recent

    # Нормализация: volume_spike = 0.0, volume_spike*3 = 1.0
    score = _clamp((volume_ratio - cfg.volume_spike) / (cfg.volume_spike * 2))
    if price_stable:
        score = min(1.0, score * 1.3)

    return score, avg_recent


# ============================================================
# Фактор 3: Дивергенция OBV
# ============================================================


def _factor_obv(
    closes: list[float],
    volumes: list[float],
    cfg: AccumulationConfig,
) -> float:
    """OBV растет, а цена падает/стоит — признак накопления."""
    n = len(closes)
    lookback = min(cfg.obv_divergence_lookback, n)
    if lookback < 10:
        return 0.0

    # Расчет OBV
    obv = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    obv_window = obv[-lookback:]
    price_window = closes[-lookback:]

    obv_slope, _ = _linear_regression(obv_window)
    price_slope, _ = _linear_regression(price_window)

    # Дивергенция: OBV растет, а цена падает или стоит
    if obv_slope <= 0:
        return 0.0

    # Нормализация наклонов
    obv_norm = obv_slope / (abs(statistics.mean(obv_window)) + 1e-12) * 100
    price_norm = abs(price_slope) / (statistics.mean(price_window) + 1e-12) * 100

    if price_norm < 0.01:
        # Цена стоит — зависимость от силы OBV
        return _clamp(obv_norm * 5, 0.0, 1.0)

    # OBV растет быстрее падения цены
    divergence_strength = obv_norm / (price_norm + obv_norm)
    return _clamp(divergence_strength * 2)


# ============================================================
# Фактор 4: Сжатие Bollinger Bands
# ============================================================


def _factor_bb(closes: list[float], cfg: AccumulationConfig) -> float:
    """Ширина BB сжалась до минимума за период."""
    if len(closes) < cfg.bb_period + 20:
        return 0.0

    upper, middle, lower = bollinger(closes, cfg.bb_period, cfg.bb_dev)
    if not middle:
        return 0.0

    # Ширина BB в каждый момент
    bb_widths = [
        (upper[i] - lower[i]) / middle[i] if middle[i] > 0 else 0.0
        for i in range(len(middle))
    ]

    if len(bb_widths) < 20:
        return 0.0

    current_width = bb_widths[-1]
    avg_width = statistics.mean(bb_widths)

    if avg_width <= 0:
        return 0.0

    ratio = current_width / avg_width

    if ratio >= cfg.bb_compression:
        return 0.0

    return _clamp(1.0 - ratio / cfg.bb_compression)


# ============================================================
# Фактор 5: Smart Money (Buy/Sell Ratio + крупные свечи)
# ============================================================


def _factor_smart_money(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    cfg: AccumulationConfig,
) -> tuple[float, float]:
    """Buy/Sell Volume Ratio > 1.2 + рост крупных сделков.

    Returns:
        (score, buy_sell_ratio).
    """
    n = len(closes)
    lookback = min(cfg.smart_money_lookback, n)
    if lookback < 3:
        return 0.0, 1.0

    # Buy/Sell Volume Ratio по телам свечей
    buy_volumes = []
    sell_volumes = []

    for i in range(-lookback, 0):
        candle_range = highs[i] - lows[i]
        if candle_range <= 0:
            continue
        body = closes[i] - opens[i]
        body_ratio = abs(body) / candle_range
        typical_volume = (highs[i] - lows[i]) * abs(closes[i])

        if body >= 0:
            buy_volumes.append(typical_volume * body_ratio)
            sell_volumes.append(typical_volume * (1 - body_ratio))
        else:
            buy_volumes.append(typical_volume * (1 - body_ratio))
            sell_volumes.append(typical_volume * body_ratio)

    total_buy = sum(buy_volumes) if buy_volumes else 0
    total_sell = sum(sell_volumes) if sell_volumes else 1

    buy_sell_ratio = total_buy / total_sell if total_sell > 0 else 1.0

    # EMA Buy Ratio для сглаживания
    buy_ratios = []
    for i in range(-lookback, 0):
        candle_range = highs[i] - lows[i]
        if candle_range <= 0:
            buy_ratios.append(0.5)
            continue
        body = closes[i] - opens[i]
        buy_ratios.append(0.5 + body / candle_range * 0.5)

    ema_ratio = _ema(buy_ratios, min(5, len(buy_ratios)))
    current_ratio = ema_ratio[-1] if ema_ratio else 0.5

    # Крупные свечи (> large_trade_threshold от среднего размаха)
    avg_range = statistics.mean([highs[i] - lows[i] for i in range(-lookback, 0)])
    large_count = sum(
        1
        for i in range(-lookback, 0)
        if (highs[i] - lows[i]) > avg_range * cfg.large_trade_threshold
    )
    large_factor = min(1.0, large_count / 3)

    # Итоговый score
    ratio_score = _clamp((buy_sell_ratio - 1.0) / 0.5, 0.0, 1.0)
    ema_score = _clamp((current_ratio - 0.5) / 0.3, 0.0, 1.0)

    score = ratio_score * 0.4 + ema_score * 0.3 + large_factor * 0.3
    return score, buy_sell_ratio


# ============================================================
# Фактор 6: Отток с бирж (Volume / Price heuristics)
# ============================================================


def _factor_outflow(
    closes: list[float],
    volumes: list[float],
    cfg: AccumulationConfig,
) -> float:
    """Volume spike при стабильной цене → накопление (прокси оттока)."""
    n = len(closes)
    lookback = min(cfg.volume_lookback, n // 3)
    if lookback < 5:
        return 0.0

    recent_volumes = volumes[-lookback:]
    prev_volumes = (
        volumes[-lookback * 3 : -lookback] if n >= lookback * 3 else volumes[:-lookback]
    )

    avg_recent = statistics.mean(recent_volumes)
    avg_prev = statistics.mean(prev_volumes) if prev_volumes else avg_recent

    if avg_prev <= 0:
        return 0.0

    volume_ratio = avg_recent / avg_prev

    # Стабильность цены
    recent_closes = closes[-lookback:]
    price_change = (
        abs(recent_closes[-1] - recent_closes[0]) / recent_closes[0]
        if recent_closes[0] > 0
        else 0
    )

    if volume_ratio < cfg.volume_spike or price_change > 0.05:
        return 0.0

    score = _clamp((volume_ratio - cfg.volume_spike) / (cfg.volume_spike * 2))
    return score


# ============================================================
# Фактор 7: RSI (нейтральная зона + дивергенция)
# ============================================================


def _factor_rsi(
    closes: list[float],
    cfg: AccumulationConfig,
) -> float:
    """RSI 40-55 + бычьи дивергенции."""
    if len(closes) < cfg.rsi_period + 20:
        return 0.0

    rsi_vals = rsi(closes, cfg.rsi_period)
    if not rsi_vals:
        return 0.0

    current_rsi = rsi_vals[-1]
    score = 0.0

    # Нейтральная зона 40-55
    if 40 <= current_rsi <= 55:
        score += 0.3
    elif 35 <= current_rsi <= 60:
        score += 0.15

    # Бычья дивергенция: цена падает, RSI растет
    lookback = min(20, len(rsi_vals))
    price_window = closes[-lookback:]
    rsi_window = rsi_vals[-lookback:]

    price_slope, _ = _linear_regression(price_window)
    rsi_slope, _ = _linear_regression(rsi_window)

    if price_slope < 0 and rsi_slope > 0:
        divergence = abs(rsi_slope) / (
            abs(price_slope) / statistics.mean(price_window) * 100 + 1e-12
        )
        score += _clamp(divergence * 0.5, 0.0, 0.7)

    return _clamp(score)


# ============================================================
# Фактор 8: Ликвидность (спред + объем сделок)
# ============================================================


def _factor_liquidity(
    spread_pct: float,
    trades_24h: int,
    cfg: AccumulationConfig,
) -> float:
    """Низкий спред + высокий объем сделок."""
    if spread_pct > cfg.max_spread:
        return 0.0
    if trades_24h < cfg.min_trades:
        return 0.0

    spread_score = _clamp(1.0 - spread_pct / cfg.max_spread)
    trade_score = _clamp(min(1.0, trades_24h / (cfg.min_trades * 3)))

    return spread_score * 0.5 + trade_score * 0.5


# ============================================================
# Фильтры шума
# ============================================================


def passes_noise_filters(
    candles: list[dict],
    avg_volume_usd: float,
    spread_pct: float,
    trades_24h: int,
    cfg: AccumulationConfig,
) -> tuple[bool, str]:
    """Проверка критических фильтров отсечения шума.

    Returns:
        (passes, reason) — reason пустая, если всё ОК.
    """
    if len(candles) < 10:
        return False, "мало свечей"

    closes = [c["close"] for c in candles]

    # Волатильность за 3 дня
    if len(closes) >= 4:
        vol_3d = abs(closes[-1] - closes[-4]) / closes[-4] if closes[-4] > 0 else 0
        if vol_3d > cfg.max_volatility_3d:
            return False, f"волатильность 3д={vol_3d:.1%}"

    # Объем
    if avg_volume_usd < cfg.min_volume_usd:
        return False, f"объем=${avg_volume_usd:,.0f}"

    # Спред
    if spread_pct > cfg.max_spread:
        return False, f"спред={spread_pct:.3%}"

    # Сделки
    if trades_24h < cfg.min_trades:
        return False, f"сделок={trades_24h}"

    # Падение > 30% за 7 дней
    if len(closes) >= 8:
        drop_7d = (closes[-1] - closes[-8]) / closes[-8] if closes[-8] > 0 else 0
        if drop_7d < -cfg.max_drop_7d:
            return False, f"падение 7д={drop_7d:.1%}"

    # Цена выше лимита
    if cfg.max_price > 0 and closes[-1] > cfg.max_price:
        return False, f"цена={closes[-1]:.4f} > ${cfg.max_price}"

    return True, ""


# ============================================================
# Основная функция анализа
# ============================================================


def analyze_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    cfg: AccumulationConfig | None = None,
    spread_pct: float = 0.0,
    trades_24h: int = 0,
    turnover_24h: float = 0.0,
) -> AccumulationSignal:
    """Полный анализ одного символа: 8 факторов → вероятность пампа.

    Args:
        symbol: торговая пара.
        timeframe: таймфрейм.
        candles: список свечей (от старых к новым).
        cfg: конфигурация порогов (None = дефолт).
        spread_pct: текущий спред в %.
        trades_24h: количество сделок за 24ч.
        turnover_24h: оборот за 24ч в USDT.

    Returns:
        AccumulationSignal с полными результатами анализа.
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG

    empty = AccumulationSignal(
        symbol=symbol,
        timeframe=timeframe,
        price=0.0,
        spread_pct=spread_pct,
        trades_24h=trades_24h,
        turnover_24h=turnover_24h,
    )

    if len(candles) < 30:
        return empty

    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    current_price = closes[-1]
    if current_price <= 0:
        return empty

    avg_vol_usd = (
        statistics.mean(volumes[-cfg.volume_lookback :])
        if len(volumes) >= cfg.volume_lookback
        else statistics.mean(volumes)
    )

    # --- 8 факторов ---
    f1, range_pct, range_pos = _factor_range(highs, lows, closes, cfg)
    f2, avg_volume = _factor_volume(volumes, highs, lows, cfg)
    f3 = _factor_obv(closes, volumes, cfg)
    f4 = _factor_bb(closes, cfg)
    f5, buy_sell = _factor_smart_money(opens, highs, lows, closes, cfg)
    f6 = _factor_outflow(closes, volumes, cfg)
    f7 = _factor_rsi(closes, cfg)
    f8 = _factor_liquidity(spread_pct, trades_24h, cfg)

    # --- Расчет вероятности ---
    # Если on-chain данных нет — перераспределяем вес outflow на smart_money
    w_outflow = cfg.weight_outflow
    w_smart = cfg.weight_smart_money
    if f6 == 0.0 and avg_vol_usd > 0:
        w_smart += w_outflow
        w_outflow = 0.0

    pump_probability = (
        cfg.weight_range * f1
        + cfg.weight_volume * f2
        + cfg.weight_obv * f3
        + cfg.weight_bb * f4
        + w_smart * f5
        + w_outflow * f6
        + cfg.weight_rsi * f7
        + cfg.weight_liquidity * f8
    )

    pump_probability = _clamp(pump_probability)

    # --- Статус ---
    if pump_probability > 0.75:
        status = "КРИТИЧЕСКИЙ"
    elif pump_probability > 0.60:
        status = "СИЛЬНЫЙ"
    elif pump_probability > 0.45:
        status = "УМЕРЕННЫЙ"
    else:
        status = "ШУМ"

    logger.debug(
        "%s %s: P=%.1f%% R=%.2f V=%.2f OBV=%.2f BB=%.2f SM=%.2f OF=%.2f RSI=%.2f L=%.2f",
        symbol,
        timeframe,
        pump_probability * 100,
        f1,
        f2,
        f3,
        f4,
        f5,
        f6,
        f7,
        f8,
    )

    return AccumulationSignal(
        symbol=symbol,
        timeframe=timeframe,
        price=current_price,
        range_pct=round(range_pct * 100, 1),
        range_position=round(range_pos, 2),
        avg_volume=avg_volume,
        buy_sell_ratio=round(buy_sell, 2),
        f_range=round(f1, 3),
        f_volume=round(f2, 3),
        f_obv=round(f3, 3),
        f_bb=round(f4, 3),
        f_smart_money=round(f5, 3),
        f_outflow=round(f6, 3),
        f_rsi=round(f7, 3),
        f_liquidity=round(f8, 3),
        pump_probability=round(pump_probability, 3),
        status=status,
        spread_pct=spread_pct,
        trades_24h=trades_24h,
        turnover_24h=turnover_24h,
    )


# ============================================================
# Backtest
# ============================================================


def backtest(
    candles: list[dict],
    symbol: str = "TEST",
    *,
    cfg: AccumulationConfig | None = None,
    hold_days: list[int] | None = None,
) -> dict:
    """Бэктест: прогон скринера по скользящему окну.

    Проверяет, был ли рост > 20% через 7/14/30 дней после сигнала.

    Args:
        candles: история свечей (минимум 120).
        symbol: имя для логов.
        cfg: конфигурация.
        hold_days: горизонты удержания (по умолчанию [7, 14, 30]).

    Returns:
        Метрики: signals, precision, avg_return, hold_stats.
    """
    if cfg is None:
        cfg = DEFAULT_CONFIG

    if hold_days is None:
        hold_days = [7, 14, 30]

    min_candles = cfg.analysis_period + max(hold_days) + 10
    if len(candles) < min_candles:
        return {"error": f"нужно минимум {min_candles} свечей, есть {len(candles)}"}

    signals = 0
    correct = {d: 0 for d in hold_days}
    total_return = {d: 0.0 for d in hold_days}
    all_returns: dict[int, list[float]] = {d: [] for d in hold_days}

    step = 5
    for i in range(cfg.analysis_period, len(candles) - max(hold_days), step):
        window = candles[i - cfg.analysis_period : i + 1]
        result = analyze_symbol(symbol, "1D", window, cfg=cfg)

        if result.pump_probability < cfg.min_pump_probability:
            continue

        signals += 1
        entry_price = candles[i]["close"]

        for days in hold_days:
            future_price = candles[i + days]["close"]
            ret = (future_price - entry_price) / entry_price
            total_return[days] += ret
            all_returns[days].append(ret)
            if ret > 0.20:
                correct[days] += 1

    if signals == 0:
        return {
            "signals": 0,
            "precision": {d: 0.0 for d in hold_days},
            "avg_return": {d: 0.0 for d in hold_days},
            "total_signals": 0,
        }

    precision = {d: correct[d] / signals for d in hold_days}
    avg_return = {d: total_return[d] / signals for d in hold_days}

    return {
        "symbol": symbol,
        "total_signals": signals,
        "precision": {d: f"{p:.1%}" for d, p in precision.items()},
        "avg_return": {d: f"{r:+.1%}" for d, r in avg_return.items()},
        "hold_days": hold_days,
    }
