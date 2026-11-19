"""Скринер тренда по правилам robot_trend.

Скринер отвечает только на вопрос «есть ли тренд»: старший ТФ (HTF, 1h) —
EMA_FAST > EMA_SLOW, цена > EMA_MACRO, ADX >= порога и DI в сторону тренда.
Точку входа выбирает сам робот (откат к EMA_FAST, объём, зазор EMA на 5m):
скринер лишь отдаёт ему монету, а входные условия показывает справочно,
чтобы было видно, близок ли вход.

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
    """Параметры отбора тренда (по умолчанию — как в robot_trend)."""

    ema_fast: int = 23
    ema_slow: int = 50
    ema_macro: int = 200
    adx_period: int = 14
    adx_threshold: float = 26.0
    vol_period: int = 20
    min_turnover_24h: float = 10_000_000
    max_price: float = 0.0
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


def _ltf_info(candles: list[dict], cfg: ScanConfig) -> dict[str, float | bool]:
    """Справочные данные сигнального ТФ: зазор EMA, объём, откат.

    На отбор не влияет: это условия входа робота, а не признак тренда.
    """
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    fast_vals = ema(closes, cfg.ema_fast)
    slow_vals = ema(closes, cfg.ema_slow)
    vol_ma = sma(volumes, cfg.vol_period)
    if not fast_vals or not slow_vals or not vol_ma or vol_ma[-1] <= 0:
        return {"fast": 0.0, "slow": 0.0, "spread": 0.0, "vol_ratio": 0.0,
                "pullback_long": False, "pullback_short": False}

    f, s = fast_vals[-1], slow_vals[-1]
    last = candles[-1]
    return {
        "fast": f,
        "slow": s,
        "spread": abs(f - s) / s * 100.0 if s > 0 else 0.0,
        "vol_ratio": volumes[-1] / vol_ma[-1],
        "pullback_long": last["low"] <= f < last["close"],
        "pullback_short": last["high"] >= f > last["close"],
    }


def analyze_symbol(
    symbol: str,
    candles: list[dict],
    *,
    htf_candles: list[dict],
    cfg: ScanConfig | None = None,
    timeframe: str = "5",
    turnover_24h: float = 0.0,
    htf_timeframe: str = "60",
) -> ScanResult | None:
    """Найти тренд по HTF EMA/ADX/DI; входные условия — только для справки.

    Args:
        symbol: имя пары.
        candles: свечи сигнального ТФ [{open_time, open, high, low, close, volume}].
        htf_candles: свечи старшего ТФ — обязательны, тренд считается только по ним.
        cfg: конфигурация.
        timeframe: таймфрейм сигнала.
        turnover_24h: оборот за 24ч.
        htf_timeframe: таймфрейм старшего ТФ (для отчёта).

    Returns:
        ScanResult или None если тренда нет.
    """
    if cfg is None:
        cfg = ScanConfig()

    found = _ema_trend(htf_candles, cfg)
    if found is None:
        return None
    trend, metrics = found

    last = candles[-1]
    if cfg.max_price > 0 and last["close"] > cfg.max_price:
        return None

    info = _ltf_info(candles, cfg)
    pullback = bool(
        info["pullback_long"] if trend == "UP" else info["pullback_short"]
    )
    score = int(min(100.0, metrics["adx"] * 2.0 + float(info["spread"]) * 5.0))

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=last["close"],
        trend=trend,
        ema_fast=float(info["fast"]),
        ema_slow=float(info["slow"]),
        ema_macro=metrics["ema_macro"],
        spread_pct=float(info["spread"]),
        adx_value=metrics["adx"],
        di_plus=metrics["di_plus"],
        di_minus=metrics["di_minus"],
        score=score,
        turnover_24h=turnover_24h,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        htf_timeframe=htf_timeframe,
        htf_adx=metrics["adx"],
        vol_ratio=float(info["vol_ratio"]),
        pullback=pullback,
    )
