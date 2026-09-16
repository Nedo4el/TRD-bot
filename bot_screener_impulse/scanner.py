"""Скринер импульсов: свеча с движением >= N% (по телу).

Логика:
- LONG: (close - open) / open * 100 >= MIN_MOVE_PCT
- SHORT: (open - close) / open * 100 >= MIN_MOVE_PCT
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

from core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ImpulseConfig:
    min_move_pct: float = 4.0
    confirmation_candles: int = 2


@dataclass
class ImpulseSignal:
    symbol: str
    timeframe: str
    price: float
    direction: str = ""
    move_pct: float = 0.0
    confirmed: bool = False
    turnover_24h: float = 0.0
    signal_time: str = ""


def analyze_symbol(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    *,
    cfg: ImpulseConfig | None = None,
    turnover_24h: float = 0.0,
) -> ImpulseSignal | None:
    if cfg is None:
        cfg = ImpulseConfig()

    min_candles = cfg.confirmation_candles + 2
    if len(candles) < min_candles:
        return None

    search_end = len(candles) - cfg.confirmation_candles

    for i in range(search_end - 1, 0, -1):
        c = candles[i]
        op = float(c["open"])
        cl = float(c["close"])

        if op <= 0:
            continue

        body = cl - op
        move_pct = abs(body) / op * 100

        if move_pct < cfg.min_move_pct:
            continue

        direction = "LONG" if cl > op else "SHORT"

        confirmed = _check_confirmation(candles, i, cfg.confirmation_candles, direction)
        if not confirmed:
            continue

        return ImpulseSignal(
            symbol=symbol,
            timeframe=timeframe,
            price=cl,
            direction=direction,
            move_pct=round(move_pct, 2),
            confirmed=True,
            turnover_24h=turnover_24h,
            signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        )

    return None


def _check_confirmation(
    candles: list[dict],
    impulse_idx: int,
    num_candles: int,
    direction: str,
) -> bool:
    """Следующие свечи не перекрывают тело импульса."""
    body_top = max(float(candles[impulse_idx]["open"]), float(candles[impulse_idx]["close"]))
    body_bottom = min(float(candles[impulse_idx]["open"]), float(candles[impulse_idx]["close"]))

    for j in range(1, num_candles + 1):
        idx = impulse_idx + j
        if idx >= len(candles):
            return False

        c = candles[idx]
        if direction == "LONG" and float(c["low"]) < body_bottom:
            return False
        if direction == "SHORT" and float(c["high"]) > body_top:
            return False

    return True
