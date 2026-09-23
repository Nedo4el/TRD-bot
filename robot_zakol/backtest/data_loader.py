"""Загрузка данных: tick > 1s > 1m (с пометкой fidelity)."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from core.bybit_client import BybitClient, Candle

logger = logging.getLogger(__name__)

FIDELITY_TICK = "TICK"
FIDELITY_1S = "1S"
FIDELITY_1M = "1M"
FIDELITY_LOW = "LOW"


class Fidelity(str, Enum):
    """Качество входных данных для fill-модели."""

    TICK = "TICK"
    S1 = "1S"
    M1 = "1M"
    LOW = "LOW"


@dataclass(frozen=True)
class Tick:
    """Один тик (или синтетический под-тик свечи)."""

    ts_ms: int
    price: float
    size: float
    side: str = "buy"


@dataclass
class Series:
    """Готовый тик-ряд + метаданные fidelity."""

    ticks: list[Tick]
    fidelity: str
    source_note: str
    candle_count: int = 0

    @property
    def is_low_fidelity(self) -> bool:
        return self.fidelity in (FIDELITY_1M, FIDELITY_LOW)


def parse_ms(date_str: str) -> int:
    """YYYY-MM-DD → unix ms (UTC, начало дня)."""
    return int(
        datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        * 1000,
    )


def end_of_day_ms(date_str: str) -> int:
    """YYYY-MM-DD → unix ms конца дня (23:59:59.999)."""
    return parse_ms(date_str) + 86400000 - 1


async def fetch_candles(
    client: BybitClient,
    symbol: str,
    interval: str,
    limit: int,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[Candle]:
    """Свечи с пагинацией назад от end_ms (max 1000/запрос)."""
    page = 1000
    all_candles: list[Candle] = []
    remaining = limit
    while remaining > 0:
        batch = min(remaining, page)
        end_ts = end_ms
        if all_candles:
            end_ts = all_candles[0].open_time - 1
        chunk = await client.get_klines(
            symbol,
            interval=interval,
            limit=batch,
            end=end_ts,
        )
        if not chunk:
            break
        all_candles = chunk + all_candles
        remaining -= len(chunk)
        if len(chunk) < batch:
            break
        if start_ms is not None and chunk[0].open_time <= start_ms:
            break
    if start_ms is not None:
        all_candles = [c for c in all_candles if c.open_time >= start_ms]
    if end_ms is not None:
        all_candles = [c for c in all_candles if c.open_time <= end_ms]
    return all_candles


def candles_to_ticks(candles: list[Candle], interval: str) -> list[Tick]:
    """Синтезировать тики из OHLC: путь open→extreme→close, 60 под-тиков/бар.

    Для interval='1' (1 мин) ~1 под-тик/с — компромисс вместо publicTrade.
    Явно помечается fidelity в Series (LOW / 1S-approx).
    """
    ticks: list[Tick] = []
    bar_ms = _bar_ms(interval)
    n = max(min(bar_ms // 1000, 120), 8)
    for c in candles:
        path = _ohlc_path(c.open, c.high, c.low, c.close, n=n)
        for i, price in enumerate(path):
            ts = c.open_time + int(i * bar_ms / max(len(path), 1))
            size = c.volume / max(len(path), 1)
            side = "buy" if i and price >= path[i - 1] else "sell"
            ticks.append(Tick(ts_ms=ts, price=price, size=size, side=side))
    ticks.sort(key=lambda t: t.ts_ms)
    return ticks


def _ohlc_path(o: float, h: float, l: float, c: float, n: int = 8) -> list[float]:
    """Путь внутри бара: open → high/low линейно → close (без разрывов)."""
    if n <= 2:
        return [o, c]
    up = c >= o
    first_ext, second_ext = (h, l) if up else (l, h)
    path = [o]
    seg = max((n - 2) // 2, 1)
    for i in range(seg):
        t = (i + 1) / seg
        path.append(o + (first_ext - o) * t)
    for i in range(seg):
        t = (i + 1) / seg
        path.append(first_ext + (second_ext - first_ext) * t)
    while len(path) < n - 1:
        path.append(second_ext)
    path.append(c)
    return path[:n]


def _bar_ms(interval: str) -> int:
    unit = interval[-1] if interval and interval[-1].isalpha() else ""
    num = int(interval[:-1]) if unit else int(interval or "1")
    scale = {"s": 1000, "m": 60000, "H": 3600000, "D": 86400000}.get(unit, 60000)
    if not unit:
        scale = 60000
    return num * scale


async def load_series(
    client: BybitClient,
    symbol: str,
    interval: str,
    limit: int,
    start_ms: int | None,
    end_ms: int | None,
) -> Series:
    """Выбрать лучший доступный источник и вернуть Series с fidelity."""
    for cand in ("1", "5", "15"):
        try:
            candles = await fetch_candles(
                client,
                symbol,
                cand,
                limit,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("kline interval=%s failed: %s", cand, exc)
            continue
        if not candles:
            continue
        fidelity = FIDELITY_1S if cand == "1" else FIDELITY_1M
        note = f"source=kline interval={cand} bars={len(candles)} fidelity={fidelity}"
        if cand != "1":
            note += " LOW FIDELITY BACKTEST (нет tick/1s)"
            logger.warning("LOW FIDELITY BACKTEST: %s", note)
            fidelity = FIDELITY_LOW
        else:
            note += " (synthetic ticks from OHLC, not publicTrade)"
            logger.warning("SYNTHETIC TICKS: %s", note)
        ticks = candles_to_ticks(candles, cand)
        return Series(
            ticks=ticks,
            fidelity=fidelity,
            source_note=note,
            candle_count=len(candles),
        )
    raise RuntimeError(f"no kline data for {symbol}")


def bars_for_range(start_ms: int, end_ms: int, interval: str) -> int:
    """Оценка числа баров в диапазоне для limit."""
    ms = _bar_ms(interval)
    return max(math.ceil((end_ms - start_ms) / ms) + 1, 1)
