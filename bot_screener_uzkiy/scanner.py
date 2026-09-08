"""Screener — поиск монет с узким диапазоном на 1М.

Запуск:  python bot_screener_uzkiy/main.py
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScanConfig:
    lookback: int = 20
    candle_width_max: float = 0.59
    candle_width_min: float = 0.05
    delta_max: float = 3.4
    min_turnover_24h: float = 30_000_000


DEFAULT_CONFIG = ScanConfig()


@dataclass
class ScanResult:
    symbol: str
    timeframe: str
    price: float
    avg_width_pct: float = 0.0
    max_width_pct: float = 0.0
    avg_delta: float = 0.0
    max_delta: float = 0.0
    quiet_candles: int = 0
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

    # Берём последние N свечей
    tail = candles[-cfg.lookback:]

    widths = []
    deltas = []
    for c in tail:
        op = float(c["open"])
        cl = float(c["close"])
        hi = float(c["high"])
        lo = float(c["low"])
        vol = float(c["volume"])

        w = (hi - lo) / cl * 100 if cl > 0 else 0
        widths.append(w)

        rng = hi - lo
        if rng <= 0:
            deltas.append(0.0)
        else:
            deltas.append(abs(vol * ((cl - op) / rng)))

    avg_width = sum(widths) / len(widths)
    max_width = max(widths)
    avg_delta = sum(deltas) / len(deltas)
    max_delta = max(deltas)

    # Считаем тихие свечи: width < max AND delta < max
    quiet = 0
    for i in range(len(widths)):
        if widths[i] < cfg.candle_width_max and deltas[i] < cfg.delta_max:
            quiet += 1

    quiet_pct = quiet / len(widths)

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
        max_width_pct=round(max_width, 3),
        avg_delta=round(avg_delta, 0),
        max_delta=round(max_delta, 0),
        quiet_candles=quiet,
        total_candles=len(widths),
        turnover_24h=turnover_24h,
        status=status,
        signal_time=datetime.now(timezone.utc).strftime("%H:%M:%S"),
    )
