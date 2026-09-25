"""Анализ спайков цены: резкий прострел >=3% за <=20 секунд в лонг или шорт.

Спайк: цена за последние window_sec секунд изменилась на >= spike_pct
вверх (LONG) или вниз (SHORT). Считаются от min/max скользящего окна.

Данные: Bybit kline M1 -> synthetic 1S (60 под-тиков/бар, 1/секунду).
Исторических tick-данных у Bybit REST нет, поэтому путь внутри бара
синтезируется из OHLC (open -> extreme -> close), как в backtest fill-модели.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.logger import get_logger

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))
SUBTICKS_PER_BAR = 60
GAP_MS = 1500


@dataclass
class SpikeConfig:
    """Параметры анализа спайков."""

    spike_pct: float = 0.03
    window_sec: int = 20
    cooldown_sec: int = 60
    lookback_days: int = 30
    interval: str = "1"
    min_turnover_24h: float = 30_000_000
    exclude_symbols: list[str] = field(default_factory=list)


@dataclass
class Spike:
    """Один зафиксированный спайк."""

    ts_ms: int
    direction: str  # "LONG" | "SHORT"
    move_pct: float  # движение от экстремума окна (+ или -)
    price: float


@dataclass
class SymbolStats:
    """Статистика спайков по одному символу за период."""

    symbol: str
    bars: int = 0
    longs: int = 0
    shorts: int = 0
    max_bar_range_pct: float = 0.0
    spikes: list[Spike] = field(default_factory=list)
    hour_counts: Counter[int] = field(default_factory=Counter)  # часы, MSK
    weekday_counts: Counter[int] = field(default_factory=Counter)  # 0=пн

    @property
    def total(self) -> int:
        """Всего спайков."""
        return self.longs + self.shorts


def _bar_points(c: dict) -> list[tuple[int, float]]:
    """Синтетический путь внутри минутной свечи: 1 под-тик/секунду.

    open -> high -> low -> close (если бар ростовой),
    open -> low -> high -> close (если бар падальной).
    """
    o, h, low, close = c["open"], c["high"], c["low"], c["close"]
    n = SUBTICKS_PER_BAR
    up = close >= o
    first, second = (h, low) if up else (low, h)
    path = [o]
    seg = max((n - 2) // 2, 1)
    for k in range(1, seg + 1):
        path.append(o + (first - o) * k / seg)
    for k in range(1, seg + 1):
        path.append(first + (second - first) * k / seg)
    while len(path) < n - 1:
        path.append(second)
    path.append(close)
    return [(c["open_time"] + i * 1000, p) for i, p in enumerate(path[:n])]


class _Detector:
    """Детектор спайков по скользящему окну (min/max за window_sec)."""

    def __init__(self, cfg: SpikeConfig, stats: SymbolStats) -> None:
        self._cfg = cfg
        self._stats = stats
        self._window: deque[tuple[int, float]] = deque()
        self._last_event_ms: int = -(10**15)

    def feed(self, points: list[tuple[int, float]]) -> None:
        """Обработать под-тики одного или двух соседних баров."""
        cfg = self._cfg
        window_ms = cfg.window_sec * 1000
        cooldown_ms = cfg.cooldown_sec * 1000

        for ts, price in points:
            if price <= 0:
                continue
            if self._window and ts - self._window[-1][0] > GAP_MS:
                self._window.clear()
            self._window.append((ts, price))
            while ts - self._window[0][0] > window_ms:
                self._window.popleft()

            lo = min(p for _, p in self._window)
            hi = max(p for _, p in self._window)
            if price >= lo * (1 + cfg.spike_pct):
                direction, move = "LONG", price / lo - 1
            elif price <= hi * (1 - cfg.spike_pct):
                direction, move = "SHORT", price / hi - 1
            else:
                continue

            if ts - self._last_event_ms < cooldown_ms:
                continue
            self._last_event_ms = ts
            self._record(ts, direction, move, price)

    def _record(
        self, ts_ms: int, direction: str, move_pct: float, price: float
    ) -> None:
        msk = datetime.fromtimestamp(ts_ms / 1000, tz=MSK)
        stats = self._stats
        stats.spikes.append(
            Spike(ts_ms=ts_ms, direction=direction, move_pct=move_pct, price=price),
        )
        if direction == "LONG":
            stats.longs += 1
        else:
            stats.shorts += 1
        stats.hour_counts[msk.hour] += 1
        stats.weekday_counts[msk.weekday()] += 1


def analyze_symbol(
    symbol: str,
    candles: list[dict],
    *,
    cfg: SpikeConfig | None = None,
) -> SymbolStats | None:
    """Найти спайки >= spike_pct за window_sec на одном символе.

    Args:
        symbol: имя торговой пары.
        candles: минутные свечи [{open_time, open, high, low, close, volume, turnover}].
        cfg: параметры анализа.

    Returns:
        SymbolStats или None, если данных недостаточно.

    Prefilter: 20-секундное окно всегда лежит не более чем в двух соседних
    барах, поэтому достаточно проверить пары баров с combined range >= spike_pct.
    """
    if cfg is None:
        cfg = SpikeConfig()
    if len(candles) < 100:
        return None

    candles = sorted(candles, key=lambda c: c["open_time"])
    stats = SymbolStats(symbol=symbol, bars=len(candles))
    detector = _Detector(cfg, stats)
    last = len(candles) - 1

    for i, c in enumerate(candles):
        if c["low"] <= 0:
            continue
        bar_range = (c["high"] - c["low"]) / c["low"]
        stats.max_bar_range_pct = max(stats.max_bar_range_pct, bar_range)

        pair_high, pair_low = c["high"], c["low"]
        if i < last:
            pair_high = max(pair_high, candles[i + 1]["high"])
            pair_low = min(pair_low, candles[i + 1]["low"])
        if pair_low <= 0 or pair_high / pair_low - 1 < cfg.spike_pct:
            continue

        points = _bar_points(c)
        if i < last:
            points += _bar_points(candles[i + 1])
        detector.feed(points)

    return stats
