"""Screener — поиск монет с узким диапазоном на 1М.

Запуск:  python bot_screener_uzkiy/main.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.logger import get_logger

logger = get_logger(__name__)


def _sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(values[i - period + 1 : i + 1]) / period)
    return result


@dataclass
class ScanConfig:
    lookback: int = 20
    volume_sma_period: int = 50
    volume_max_ratio: float = 2.2
    candle_width_max: float = 0.59
    delta_sma_period: int = 20
    delta_max_ratio: float = 3.4
    min_turnover_24h: float = 30_000_000


DEFAULT_CONFIG = ScanConfig()


@dataclass
class ScanResult:
    symbol: str
    timeframe: str
    price: float
    avg_width_pct: float = 0.0
    max_vol_ratio: float = 0.0
    avg_vol_ratio: float = 0.0
    max_delta_ratio: float = 0.0
    avg_delta_ratio: float = 0.0
    small_candles: int = 0
    total_candles: int = 0
    turnover_24h: float = 0.0
    status: str = ""
    signal_time: str = ""


def analyze_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    cfg: ScanConfig | None = None,
    turnover_24h: float = 0.0,
) -> ScanResult:
    if cfg is None:
        cfg = DEFAULT_CONFIG

    empty = ScanResult(symbol=symbol, timeframe=timeframe, price=0.0)

    if len(candles) < cfg.lookback:
        return empty

    closes = [float(c["close"]) for c in candles]
    current_price = closes[-1]
    if current_price <= 0:
        return empty

    volumes = [float(c["volume"]) for c in candles]
    vol_sma = _sma(volumes, cfg.volume_sma_period)

    deltas: list[float] = []
    for c in candles:
        op = float(c["open"])
        cl = float(c["close"])
        hi = float(c["high"])
        lo = float(c["low"])
        vol = float(c["volume"])
        rng = hi - lo
        if rng <= 0:
            deltas.append(0.0)
        else:
            deltas.append(vol * ((cl - op) / rng))
    delta_sma = _sma([abs(d) for d in deltas], cfg.delta_sma_period)

    widths = []
    vol_ratios = []
    delta_ratios = []

    for i in range(len(candles)):
        c = candles[i]
        hi = float(c["high"])
        lo = float(c["low"])
        cl = float(c["close"])

        w = (hi - lo) / cl * 100 if cl > 0 else 0
        widths.append(w)

        sv = vol_sma[i]
        vr = volumes[i] / sv if sv and sv > 0 else 0
        vol_ratios.append(vr)

        sd = delta_sma[i]
        dr = abs(deltas[i]) / sd if sd and sd > 0 else 0
        delta_ratios.append(dr)

    avg_width = sum(widths) / len(widths) if widths else 0
    max_vol = max(vol_ratios) if vol_ratios else 0
    avg_vol = sum(vol_ratios) / len(vol_ratios) if vol_ratios else 0
    max_delta = max(delta_ratios) if delta_ratios else 0
    avg_delta = sum(delta_ratios) / len(delta_ratios) if delta_ratios else 0

    # Считаем свечи которые проходят все 3 условия
    quiet = 0
    for i in range(len(widths)):
        if widths[i] < cfg.candle_width_max and vol_ratios[i] < cfg.volume_max_ratio and delta_ratios[i] < cfg.delta_max_ratio:
            quiet += 1

    quiet_pct = quiet / len(widths) if widths else 0

    # Сигнал: 50%+ свечей тихие
    if quiet_pct < 0.50:
        return empty

    if quiet_pct >= 0.8:
        status = "ОЧЕНЬ УЗКИЙ"
    elif quiet_pct >= 0.6:
        status = "УЗКИЙ"
    else:
        status = "ТИХИЙ"

    return ScanResult(
        symbol=symbol,
        timeframe=timeframe,
        price=current_price,
        avg_width_pct=round(avg_width, 3),
        max_vol_ratio=round(max_vol, 1),
        avg_vol_ratio=round(avg_vol, 1),
        max_delta_ratio=round(max_delta, 1),
        avg_delta_ratio=round(avg_delta, 1),
        small_candles=quiet,
        total_candles=len(widths),
        turnover_24h=turnover_24h,
        status=status,
        signal_time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
    )
