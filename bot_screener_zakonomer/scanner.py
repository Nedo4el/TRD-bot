"""Анализ закономерностей во времени всплесков объема.

Находит всплески объема (объём в USDT > mean + 3*std или > min_volume_usd)
и анализирует паттерны по часам, минутам и дням недели.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from core.logger import get_logger

logger = get_logger(__name__)

MSK = timezone(timedelta(hours=3))


@dataclass
class ScanConfig:
    lookback_days: int = 30
    interval: str = "1"
    min_volume_usd: float = 1_000_000
    spike_std_multiplier: float = 3.0
    min_turnover_24h: float = 30_000_000
    exclude_symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])


@dataclass
class Spike:
    symbol: str
    open_time: int
    volume_usdt: float
    price: float
    hour: int
    minute: int
    weekday: str


@dataclass
class TimePattern:
    hour: int
    count: int
    pct: float


@dataclass
class ScanResult:
    symbol: str
    total_candles: int = 0
    spikes_found: int = 0
    threshold_usd: float = 0.0
    hourly_patterns: list[TimePattern] = field(default_factory=list)
    minute_patterns: list[TimePattern] = field(default_factory=list)
    weekday_patterns: list[tuple[str, int]] = field(default_factory=list)
    top_spikes: list[Spike] = field(default_factory=list)
    signal_time: str = ""


def analyze_symbol(
    symbol: str,
    candles: list[dict],
    *,
    cfg: ScanConfig | None = None,
) -> ScanResult | None:
    """Найти всплески объема и паттерны времени для одного символа.

    Args:
        symbol: имя торговой пары.
        candles: список свечей [{open_time, open, high, low, close, volume, turnover}].
        cfg: конфигурация анализа.

    Returns:
        ScanResult или None если данных недостаточно.
    """
    if cfg is None:
        cfg = ScanConfig()

    if len(candles) < 100:
        return None

    # Объём в USDT = turnover (если есть) или volume * close
    volumes_usdt: list[float] = []
    for c in candles:
        if c.get("turnover", 0) > 0:
            volumes_usdt.append(c["turnover"])
        else:
            volumes_usdt.append(c["volume"] * c["close"])

    if not volumes_usdt:
        return None

    mean_vol = sum(volumes_usdt) / len(volumes_usdt)
    std_vol = (sum((v - mean_vol) ** 2 for v in volumes_usdt) / len(volumes_usdt)) ** 0.5

    stat_threshold = mean_vol + cfg.spike_std_multiplier * std_vol
    threshold = max(stat_threshold, cfg.min_volume_usd)

    # Находим всплески
    spikes: list[Spike] = []
    for i, c in enumerate(candles):
        vol = volumes_usdt[i]
        if vol > threshold:
            dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=MSK)
            spikes.append(Spike(
                symbol=symbol,
                open_time=c["open_time"],
                volume_usdt=vol,
                price=c["close"],
                hour=dt.hour,
                minute=dt.minute,
                weekday=dt.strftime("%A"),
            ))

    if not spikes:
        return None

    # Паттерны по часам
    hour_counts = Counter(s.hour for s in spikes)
    total = len(spikes)
    hourly = sorted(
        [TimePattern(h, c, round(c / total * 100, 1)) for h, c in hour_counts.items()],
        key=lambda x: x.count,
        reverse=True,
    )

    # Паттерны по минутам
    minute_counts = Counter(s.minute for s in spikes)
    minutely = sorted(
        [TimePattern(m, c, round(c / total * 100, 1)) for m, c in minute_counts.items()],
        key=lambda x: x.count,
        reverse=True,
    )

    # Паттерны по дням недели
    weekday_counts = Counter(s.weekday for s in spikes)
    weekday_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    weekday_patterns = [(d, weekday_counts.get(d, 0)) for d in weekday_counts]
    weekday_patterns.sort(key=lambda x: x[1], reverse=True)

    # Топ-10 всплесков по объему
    top_spikes = sorted(spikes, key=lambda x: x.volume_usdt, reverse=True)[:10]

    return ScanResult(
        symbol=symbol,
        total_candles=len(candles),
        spikes_found=len(spikes),
        threshold_usd=threshold,
        hourly_patterns=sorted(
            [TimePattern(h, c, round(c / total * 100, 1)) for h, c in hour_counts.items()],
            key=lambda x: x.hour,
        ),
        minute_patterns=minutely[:10] if len(minutely) > 10 else minutely,
        weekday_patterns=weekday_patterns,
        top_spikes=top_spikes,
        signal_time=datetime.now(MSK).strftime("%H:%M:%S"),
    )
