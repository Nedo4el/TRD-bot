"""Скринер тренда по правилам входа robot_trend.

Тренд считается на старшем ТФ (HTF, 1h): EMA_FAST > EMA_SLOW, цена > EMA_MACRO,
ADX >= порога и DI в сторону тренда. Вход ищется на сигнальном ТФ (LTF, 5m):
откат к EMA_FAST, объём >= VOL_MULT x SMA(VOL_PERIOD), зазор EMA >= минимума.
То есть сигнал скринера = условие входа робота, а не просто «цена растёт».

Score = сила тренда: ADX x 2 + зазор EMA_FAST/EMA_SLOW x 5 (максимум 100).
Скринер только находит сигналы: стоп/тейк и размер позиции не считает.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.indicators import adx_di, ema, sma
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
    adx_threshold: float = 26.0
    vol_period: int = 20
    vol_mult: float = 1.0
    ema_min_spread_pct: float = 0.1
    require_pullback: bool = True
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
    htf_timeframe: str = ""
    htf_adx: float = 0.0
    vol_ratio: float = 0.0
    pullback: bool = False


def _ema_trend(
    candles: list[dict],
    cfg: ScanConfig,
) -> tuple[str, dict[str, float]] | None:
    """Тренд по EMA/ADX/DI на переданных свечах (правило robot_trend)."""
    if len(candles) < cfg.ema_macro + 2:
        return None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    fast_vals = ema(closes, cfg.ema_fast)
    slow_vals = ema(closes, cfg.ema_slow)
    macro_vals = ema(closes, cfg.ema_macro)
    adx_vals, di_plus_vals, di_minus_vals = adx_di(
        highs, lows, closes, cfg.adx_period
    )
    if any(not vals for vals in (fast_vals, slow_vals, macro_vals, adx_vals)):
        return None

    close = closes[-1]
    f, s, m = fast_vals[-1], slow_vals[-1], macro_vals[-1]
    adx_val = adx_vals[-1]
    di_p = di_plus_vals[-1]
    di_m = di_minus_vals[-1]
    spread_pct = abs(f - s) / s * 100.0 if s > 0 else 0.0

    up = f > s and close > m and adx_val >= cfg.adx_threshold and di_p > di_m
    down = f < s and close < m and adx_val >= cfg.adx_threshold and di_m > di_p
    if not up and not down:
        return None

    return ("UP" if up else "DOWN"), {
        "price": close,
        "ema_fast": f,
        "ema_slow": s,
        "ema_macro": m,
        "spread_pct": spread_pct,
        "adx": adx_val,
        "di_plus": di_p,
        "di_minus": di_m,
    }


def _ltf_entry(
    candles: list[dict],
    cfg: ScanConfig,
    side: str,
) -> tuple[bool, float, float, float, float]:
    """Условия входа на сигнальном ТФ: откат, объём, зазор EMA.

    Returns:
        (прошёл ли фильтр, vol_ratio, зазор EMA в %, EMA_FAST, EMA_SLOW).
    """
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    fast_vals = ema(closes, cfg.ema_fast)
    slow_vals = ema(closes, cfg.ema_slow)
    vol_ma = sma(volumes, cfg.vol_period)
    if not fast_vals or not slow_vals or not vol_ma or vol_ma[-1] <= 0:
        return False, 0.0, 0.0, 0.0, 0.0

    f, s = fast_vals[-1], slow_vals[-1]
    spread = abs(f - s) / s * 100.0 if s > 0 else 0.0
    vol_ratio = volumes[-1] / vol_ma[-1]
    last = candles[-1]

    if vol_ratio < cfg.vol_mult:
        return False, vol_ratio, spread, f, s
    if cfg.ema_min_spread_pct > 0 and spread < cfg.ema_min_spread_pct:
        return False, vol_ratio, spread, f, s
    if not cfg.require_pullback:
        return True, vol_ratio, spread, f, s

    if side == "UP":
        ok = last["low"] <= f < last["close"] and f > s
    else:
        ok = last["high"] >= f > last["close"] and f < s
    return ok, vol_ratio, spread, f, s


def analyze_symbol(
    symbol: str,
    candles: list[dict],
    *,
    cfg: ScanConfig | None = None,
    timeframe: str = "5",
    turnover_24h: float = 0.0,
    htf_candles: list[dict] | None = None,
    htf_timeframe: str = "60",
) -> ScanResult | None:
    """Найти тренд по правилам robot_trend (HTF-тренд + LTF-откат).

    Args:
        symbol: имя пары.
        candles: свечи сигнального ТФ [{open_time, open, high, low, close, volume}].
        cfg: конфигурация.
        timeframe: таймфрейм сигнала.
        turnover_24h: оборот за 24ч.
        htf_candles: свечи старшего ТФ; без них тренд считается по candles.
        htf_timeframe: таймфрейм старшего ТФ (для отчёта).

    Returns:
        ScanResult или None если тренда/входа нет.
    """
    if cfg is None:
        cfg = ScanConfig()

    trend_candles = htf_candles if htf_candles else candles
    found = _ema_trend(trend_candles, cfg)
    if found is None:
        return None
    trend, metrics = found

    entry_ok, vol_ratio, ltf_spread, ltf_fast, ltf_slow = _ltf_entry(
        candles, cfg, trend
    )
    if not entry_ok:
        return None

    last = candles[-1]
    if trend == "UP":
        pullback = last["low"] <= ltf_fast < last["close"]
    else:
        pullback = last["high"] >= ltf_fast > last["close"]
    score = int(min(100.0, metrics["adx"] * 2.0 + ltf_spread * 5.0))

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=last["close"],
        trend=trend,
        ema_fast=ltf_fast,
        ema_slow=ltf_slow,
        ema_macro=metrics["ema_macro"],
        spread_pct=ltf_spread,
        adx_value=metrics["adx"],
        di_plus=metrics["di_plus"],
        di_minus=metrics["di_minus"],
        score=score,
        turnover_24h=turnover_24h,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        htf_timeframe=htf_timeframe if htf_candles else timeframe,
        htf_adx=metrics["adx"] if htf_candles else 0.0,
        vol_ratio=vol_ratio,
        pullback=pullback,
    )
