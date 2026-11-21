"""Кластеры POC-уровней: слияние 5 %, ближайший кластер, зона 10 %.

Модель «ближайший кластер» (ТЗ 22.11.2026): уровни, отстоящие друг от друга
по формуле ``|A-B| / min(A,B) < merge_pct``, объединяются в кластер
транзитивно (A~B и B~C -> все три в одном кластере). Торговый уровень
кластера — среднее арифметическое цен его участников с округлением до 0.1 %
от цены (4 значащие цифры). Скринер мониторит только ближайший к цене
кластер; сигнал появляется, когда цена входит в зону ``zone_pct`` (10 %),
которая считается от цены до уровня.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from bot_screener_yrovni.poc_scanner.store import LevelState

DEFAULT_MERGE_PCT = 0.05
DEFAULT_ZONE_PCT = 0.10
DIRECTION_LONG = "long"
DIRECTION_SHORT = "short"


@dataclass(frozen=True, slots=True)
class Cluster:
    """Кластер уровней и его торговый уровень (среднее цен)."""

    level: float
    members: tuple[float, ...]
    windows: tuple[str, ...]
    volume: float

    @property
    def size(self) -> int:
        """Сколько уровней попало в кластер."""
        return len(self.members)


def within_merge(a: float, b: float, merge_pct: float) -> bool:
    """``|A-B| / min(A,B) < merge_pct`` — уровни из одного кластера."""
    base = min(a, b)
    return base > 0 and abs(a - b) / base < merge_pct


def cluster_prices(
    prices: Iterable[float],
    merge_pct: float = DEFAULT_MERGE_PCT,
) -> list[list[float]]:
    """Разбить цены на кластеры транзитивно (single-linkage).

    Цены сортируются по возрастанию; очередная цена присоединяется к
    последнему кластеру, если близка к его последнему участнику. Так
    цепочка A~B, B~C даёт один кластер, даже если A и C дальше порога.
    """
    ordered = sorted(float(price) for price in prices if float(price) > 0)
    groups: list[list[float]] = []
    for price in ordered:
        if groups and within_merge(price, groups[-1][-1], merge_pct):
            groups[-1].append(price)
        else:
            groups.append([price])
    return groups


def round_level(price: float) -> float:
    """Округлить торговый уровень до 0.1 % от цены (4 значащие цифры)."""
    if price <= 0:
        return 0.0
    return float(f"{price:.4g}")


def build_clusters(
    levels: Iterable[LevelState],
    merge_pct: float = DEFAULT_MERGE_PCT,
) -> list[Cluster]:
    """Собрать кластеры: среднее цен, суммарный объём, все источники."""
    by_price = {
        float(level.poc_price): level for level in levels if level.poc_price > 0
    }
    clusters: list[Cluster] = []
    for group in cluster_prices(by_price, merge_pct):
        windows: set[str] = set()
        volume = 0.0
        for price in group:
            state = by_price[price]
            windows.update(state.source_windows)
            volume += float(state.total_volume_at_level)
        clusters.append(
            Cluster(
                level=round_level(sum(group) / len(group)),
                members=tuple(group),
                windows=tuple(sorted(windows)),
                volume=volume,
            )
        )
    return clusters


def zone_distance(price: float, level: float) -> float:
    """Расстояние от цены до уровня в долях цены (ТЗ: считается от цены)."""
    if price <= 0:
        return 0.0
    return abs(price - level) / price


def direction_for(price: float, level: float) -> str | None:
    """Направление входа: цена сверху — long, снизу — short."""
    if level < price:
        return DIRECTION_LONG
    if level > price:
        return DIRECTION_SHORT
    return None


def nearest_cluster(clusters: Iterable[Cluster], price: float) -> Cluster | None:
    """Ближайший к цене кластер — единственный, который мониторится."""
    best: Cluster | None = None
    best_distance = 0.0
    for cluster in clusters:
        distance = zone_distance(price, cluster.level)
        if best is None or distance < best_distance:
            best, best_distance = cluster, distance
    return best
