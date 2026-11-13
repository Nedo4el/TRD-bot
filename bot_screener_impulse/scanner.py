"""Скринер импульсов: резкое движение цены за rolling-окно.

Режимы:
- "candle" — тело свечи (close - open) / open >= min_move_pct
- "price"  — движение цены за rolling-окно window_sec (5 или 60 сек)

Логика:
- LONG: цена выросла на min_move_pct и более за окно
- SHORT: цена упала на min_move_pct и более за окно
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
    confirmation_candles: int = 0
    window_sec: int = 60
    impulse_type: str = "price"


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
    window_sec: int = 60


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

    if cfg.impulse_type == "price":
        return _analyze_price_window(symbol, timeframe, candles, cfg, turnover_24h)
    return _analyze_candle_body(symbol, timeframe, candles, cfg, turnover_24h)


def _analyze_price_window(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    cfg: ImpulseConfig,
    turnover_24h: float,
) -> ImpulseSignal | None:
    """Движение цены за rolling-окно window_sec (последние свечи)."""
    if len(candles) < 2:
        return None

    now_ms = candles[-1]["open_time"]
    window_ms = cfg.window_sec * 1000
    cutoff_ms = now_ms - window_ms

    # Находим самую старую свечу, которая ещё попадает в окно
    base_price: float | None = None
    for i in range(len(candles) - 1, -1, -1):
        if candles[i]["open_time"] <= cutoff_ms:
            base_price = float(candles[i]["close"])
            break

    if base_price is None or base_price <= 0:
        # Окно не покрывается историей — берём первую доступную свечу
        base_price = float(candles[0]["close"])

    current_price = float(candles[-1]["close"])
    if current_price <= 0:
        return None

    move_pct = (current_price - base_price) / base_price * 100.0
    if abs(move_pct) < cfg.min_move_pct:
        return None

    direction = "LONG" if move_pct > 0 else "SHORT"

    return ImpulseSignal(
        symbol=symbol,
        timeframe=timeframe,
        price=current_price,
        direction=direction,
        move_pct=round(abs(move_pct), 2),
        confirmed=True,
        turnover_24h=turnover_24h,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
        window_sec=cfg.window_sec,
    )


def _analyze_candle_body(
    symbol: str,
    timeframe: str,
    candles: list[dict],
    cfg: ImpulseConfig,
    turnover_24h: float,
) -> ImpulseSignal | None:
    """Тело свечи (close - open) / open >= min_move_pct."""
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
    body_top = max(
        float(candles[impulse_idx]["open"]), float(candles[impulse_idx]["close"])
    )
    body_bottom = min(
        float(candles[impulse_idx]["open"]), float(candles[impulse_idx]["close"])
    )

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
