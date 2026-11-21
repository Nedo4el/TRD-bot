"""Volume Profile: POC и Value Area по свечам одного окна.

Объём свечи распределяется по ценовым корзинам **пропорционально
перекрытию** отрезка [low, high] с корзиной. Это точнее, чем делить
поровну между затронутыми корзинами: свеча, краем задевшая корзину,
отдаёт ей мало объёма.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_CANDLES = 2


@dataclass(frozen=True)
class ProfileResult:
    """Итог профиля объёма для одного окна."""

    poc: float
    val: float
    vah: float
    total_volume: float


def candle_weights(df: pd.DataFrame) -> np.ndarray:
    """Оборот каждой свечи в USD: ``turnover``, иначе ``volume * close``."""
    volume = df["volume"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    turnover = (
        df["turnover"].to_numpy(dtype=float)
        if "turnover" in df.columns
        else np.zeros(len(df), dtype=float)
    )
    weights = np.where(turnover > 0, turnover, volume * close)
    return np.clip(weights, 0.0, None)


def volume_profile(
    df: pd.DataFrame,
    num_bins: int = 100,
) -> tuple[np.ndarray, np.ndarray]:
    """Разложить объём по корзинам цен.

    Args:
        df: свечи с колонками open/high/low/close/volume[/turnover].
        num_bins: число корзин на диапазон [min_low, max_high].

    Returns:
        (центры корзин, объём по корзинам) — оба массива длины num_bins
        и по возрастанию цены; пустые массивы, если профиль не построить.
    """
    if len(df) < MIN_CANDLES or num_bins < 2:
        return np.empty(0), np.empty(0)

    low = df["low"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    weight = candle_weights(df)

    price_min = float(np.min(low))
    price_max = float(np.max(high))
    if price_max <= price_min:
        return np.empty(0), np.empty(0)

    edges = np.linspace(price_min, price_max, num_bins + 1)
    left = edges[:-1][None, :]
    right = edges[1:][None, :]

    # Перекрытие каждой свечи с каждой корзиной: (n_candles, num_bins).
    overlap = np.clip(high[:, None], left, right) - np.clip(low[:, None], left, right)
    overlap = np.clip(overlap, 0.0, None)
    span = high - low
    safe_span = np.where(span > 0, span, 1.0)
    volumes = (weight[:, None] * overlap / safe_span[:, None]).sum(axis=0)

    # Свечи без диапазона (high == low) перекрытия не дают — их объём
    # кладём в корзину, где стоит цена.
    flat = span <= 0
    if bool(np.any(flat)):
        flat_bins = np.clip(
            np.searchsorted(edges, low[flat], side="right") - 1,
            0,
            num_bins - 1,
        )
        np.add.at(volumes, flat_bins, weight[flat])

    centers = (edges[:-1] + edges[1:]) / 2.0
    return centers, volumes


def find_value_area(
    centers: np.ndarray,
    volumes: np.ndarray,
    pct: float = 0.68,
) -> tuple[float, float] | None:
    """Value Area: (val, vah) — границы диапазона вокруг POC на ``pct`` объёма.

    Расширяемся от корзины POC вверх/вниз, каждый раз добавляя более
    объёмного соседа (алгоритм Market Profile).
    """
    if len(centers) == 0 or len(volumes) != len(centers):
        return None
    total = float(volumes.sum())
    if total <= 0:
        return None

    start = int(np.argmax(volumes))
    lo = hi = start
    acc = float(volumes[start])
    target = total * min(max(pct, 0.0), 1.0)

    while acc < target and (lo > 0 or hi < len(volumes) - 1):
        down = float(volumes[lo - 1]) if lo > 0 else -1.0
        up = float(volumes[hi + 1]) if hi + 1 < len(volumes) else -1.0
        if up >= down:
            hi += 1
            acc += float(volumes[hi])
        else:
            lo -= 1
            acc += float(volumes[lo])
    return float(centers[lo]), float(centers[hi])


def compute_profile(
    df: pd.DataFrame,
    num_bins: int = 100,
    va_percent: float = 0.68,
) -> ProfileResult | None:
    """POC и Value Area по свечам окна; None — профиль не построился."""
    centers, volumes = volume_profile(df, num_bins)
    area = find_value_area(centers, volumes, va_percent)
    if len(centers) == 0 or area is None:
        return None
    return ProfileResult(
        poc=float(centers[int(np.argmax(volumes))]),
        val=area[0],
        vah=area[1],
        total_volume=float(volumes.sum()),
    )
