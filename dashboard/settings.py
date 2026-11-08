"""Настройки «Все монеты»: JSON-хранилище + dataclass."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from dashboard.config import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"

_VALID_SORT_MODES = (
    "atr_desc",
    "atr_asc",
    "change_desc",
    "change_asc",
    "turnover_desc",
    "vol_trend_desc",
    "vol_trend_asc",
)
_VALID_TREND_FILTERS = ("all", "up", "down")
_VALID_INTERVALS = (30, 60, 120, 300)


@dataclass(slots=True)
class AllCoinsSettings:
    """Настройки скринера «Все монеты» (панель в дашборде)."""

    enabled: bool = True
    interval_sec: int = 60
    timeframe: str = "5"
    atr_period: int = 14
    min_turnover_24h: float = 10_000_000
    max_symbols: int = 100
    exclude_symbols: list[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])
    min_abs_change_24h: float = 0.0
    volume_trend: str = "all"
    sort_mode: str = "atr_desc"

    @classmethod
    def load(cls) -> AllCoinsSettings:
        """Прочитать JSON, при ошибке — дефолты."""
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_dict(raw)

    def save(self) -> None:
        """Атомарно записать JSON."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_FILE.with_suffix(".tmp")
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        temp.write_text(payload, encoding="utf-8")
        temp.replace(SETTINGS_FILE)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AllCoinsSettings:
        """Собрать настройки из произвольного dict (API-вход)."""
        defaults = cls()
        enabled = bool(raw.get("enabled", defaults.enabled))
        interval = int(raw.get("interval_sec", defaults.interval_sec))
        if interval not in _VALID_INTERVALS:
            interval = defaults.interval_sec
        timeframe = str(raw.get("timeframe", defaults.timeframe))
        atr_period = int(raw.get("atr_period", defaults.atr_period))
        min_turnover = float(raw.get("min_turnover_24h", defaults.min_turnover_24h))
        max_symbols = int(raw.get("max_symbols", defaults.max_symbols))
        exclude = [
            str(s).upper() for s in raw.get("exclude_symbols", defaults.exclude_symbols)
        ]
        min_abs_change = float(
            raw.get("min_abs_change_24h", defaults.min_abs_change_24h)
        )
        volume_trend = str(raw.get("volume_trend", defaults.volume_trend))
        if volume_trend not in _VALID_TREND_FILTERS:
            volume_trend = defaults.volume_trend
        sort_mode = str(raw.get("sort_mode", defaults.sort_mode))
        if sort_mode not in _VALID_SORT_MODES:
            sort_mode = defaults.sort_mode
        return cls(
            enabled=enabled,
            interval_sec=interval,
            timeframe=timeframe,
            atr_period=atr_period,
            min_turnover_24h=min_turnover,
            max_symbols=max(1, min(500, max_symbols)),
            exclude_symbols=exclude,
            min_abs_change_24h=min_abs_change,
            volume_trend=volume_trend,
            sort_mode=sort_mode,
        )

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать для JSON-API."""
        return asdict(self)
