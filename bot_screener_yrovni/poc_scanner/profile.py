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
# Узел считается значимым, только если его корзина хотя бы на 20 % плотнее
# средней: у плоского профиля локальных узлов нет.
MIN_NODE_CONCENTRATION = 1.2


@dataclass(frozen=True)
class VolumeNode:
    """Второстепенный объёмный узел профиля (HVN)."""

    price: float
    volume: float
    share: float  # объём корзины / объём корзины POC


@dataclass(frozen=True)
class ProfileResult:
    """Итог профиля объёма для одного окна."""

    poc: float
    val: float
    vah: float
    total_volume: float
    nodes: tuple[VolumeNode, ...] = ()


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


def local_peaks(volumes: np.ndarray) -> list[int]:
    """Индексы локальных максимумов; плато равных значений даёт один индекс."""
    peaks: list[int] = []
    index = 0
    count = len(volumes)
    while index < count:
        end = index
        while end + 1 < count and volumes[end + 1] == volumes[index]:
            end += 1
        left_ok = index == 0 or volumes[index - 1] < volumes[index]
        right_ok = end == count - 1 or volumes[end + 1] < volumes[end]
        if left_ok and right_ok:
            peaks.append((index + end) // 2)
        index = end + 1
    return peaks


def volume_nodes(
    centers: np.ndarray,
    volumes: np.ndarray,
    *,
    min_share: float = 0.35,
    max_nodes: int = 6,
    min_concentration: float = MIN_NODE_CONCENTRATION,
) -> tuple[VolumeNode, ...]:
    """Прочие объёмные узлы профиля: локальные максимумы ниже POC-корзины.

    Узел значим, если его объём не ниже ``min_share`` от POC-корзины и не
    ниже ``min_concentration`` среднего объёма корзины: второе условие
    отсекает плоские профили, где «узлов» нет.
    """
    if len(centers) == 0 or len(volumes) != len(centers) or max_nodes < 1:
        return ()
    total = float(volumes.sum())
    if total <= 0:
        return ()
    poc_index = int(np.argmax(volumes))
    poc_volume = float(volumes[poc_index])
    if poc_volume <= 0:
        return ()
    average = total / len(volumes)
    share_limit = poc_volume * min(max(min_share, 0.0), 1.0)
    density_limit = average * max(min_concentration, 0.0)
    indexes = [
        index
        for index in local_peaks(volumes)
        if index != poc_index
        and float(volumes[index]) >= share_limit
        and float(volumes[index]) >= density_limit
    ]
    indexes.sort(key=lambda index: float(volumes[index]), reverse=True)
    return tuple(
        VolumeNode(
            price=float(centers[index]),
            volume=float(volumes[index]),
            share=float(volumes[index]) / poc_volume,
        )
        for index in indexes[:max_nodes]
    )


def compute_profile(
    df: pd.DataFrame,
    num_bins: int = 100,
    va_percent: float = 0.68,
    *,
    node_min_share: float = 0.0,
    max_nodes: int = 0,
) -> ProfileResult | None:
    """POC, Value Area и (опционально) объёмные узлы окна.

    Args:
        df: свечи окна.
        num_bins: число корзин профиля.
        va_percent: доля объёма в Value Area.
        node_min_share: мин. доля узла от POC-корзины; 0 — узлы не считаются.
        max_nodes: сколько узлов вернуть, кроме POC.

    Returns:
        Профиль или None, если он не построился.
    """
    centers, volumes = volume_profile(df, num_bins)
    area = find_value_area(centers, volumes, va_percent)
    if len(centers) == 0 or area is None:
        return None
    nodes: tuple[VolumeNode, ...] = ()
    if max_nodes > 0 and node_min_share > 0:
        nodes = volume_nodes(
            centers,
            volumes,
            min_share=node_min_share,
            max_nodes=max_nodes,
        )
    return ProfileResult(
        poc=float(centers[int(np.argmax(volumes))]),
        val=area[0],
        vah=area[1],
        total_volume=float(volumes.sum()),
        nodes=nodes,
    )
