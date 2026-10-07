"""Volume Profile: POC и Value Area (VAH/VAL) по свечам.

POC — цена корзины с максимальным объёмом; Value Area — диапазон
крайних корзин, покрывший pct (70%) всего объёма профиля.

Порт build_volume_profile/find_poc из bot_screener_yrovni/scanner.py
(100 бинов, объём распределяется по high/low равномерно) + value area,
которой в скринере не было.
"""

from __future__ import annotations

from core.bybit_client import Candle


def build_volume_profile(
    candles: list[Candle],
    num_bins: int = 100,
) -> list[tuple[float, float]]:
    """Распределить объём свечей по ценовым корзинам.

    Args:
        candles: свечи рабочего ТФ.
        num_bins: количество корзин на весь диапазон high/low.

    Returns:
        [(цена_центра, объём), ...] по возрастанию цены;
        [] — недостаточно данных или вырожденный диапазон.
    """
    if len(candles) < 2 or num_bins < 2:
        return []
    price_min = min(c.low for c in candles)
    price_max = max(c.high for c in candles)
    if price_max <= price_min:
        return []
    bin_width = (price_max - price_min) / num_bins
    volume_by_bin: dict[int, float] = {}

    for c in candles:
        if c.volume <= 0 or c.high <= c.low:
            continue
        low_bin = max(0, min(int((c.low - price_min) / bin_width), num_bins - 1))
        high_bin = max(0, min(int((c.high - price_min) / bin_width), num_bins - 1))
        per_bin = c.volume / (high_bin - low_bin + 1)
        for b in range(low_bin, high_bin + 1):
            volume_by_bin[b] = volume_by_bin.get(b, 0.0) + per_bin

    if not volume_by_bin:
        return []
    return [
        (price_min + b * bin_width + bin_width / 2.0, vol)
        for b, vol in sorted(volume_by_bin.items())
    ]


def find_poc(profile: list[tuple[float, float]]) -> float | None:
    """Цена корзины с максимальным объёмом (None — пустой профиль)."""
    if not profile:
        return None
    return max(profile, key=lambda item: item[1])[0]


def find_value_area(
    profile: list[tuple[float, float]],
    pct: float = 0.7,
) -> tuple[float, float] | None:
    """Value Area: (val, vah) — цены крайних корзин, покрывших pct объёма.

    Алгоритм Market Profile: старт от POC, на каждом шаге добавляем
    более объёмную из двух соседних корзин, пока не наберём pct.
    """
    if not profile or pct <= 0:
        return None
    total = sum(v for _, v in profile)
    if total <= 0:
        return None
    target = total * min(pct, 1.0)
    start = max(range(len(profile)), key=lambda i: profile[i][1])
    lo = hi = start
    acc = profile[start][1]
    while acc < target:
        down = profile[lo - 1][1] if lo > 0 else -1.0
        up = profile[hi + 1][1] if hi + 1 < len(profile) else -1.0
        if up < 0 and down < 0:
            break
        if up >= down:
            hi += 1
            acc += profile[hi][1]
        else:
            lo -= 1
            acc += profile[lo][1]
    return profile[lo][0], profile[hi][0]


def compute_poc_va(
    candles: list[Candle],
    num_bins: int = 100,
    pct: float = 0.7,
) -> tuple[float, float, float] | None:
    """(poc, val, vah) по свечам окна или None — профиль не построился."""
    profile = build_volume_profile(candles, num_bins)
    poc = find_poc(profile)
    area = find_value_area(profile, pct)
    if poc is None or area is None:
        return None
    return poc, area[0], area[1]
