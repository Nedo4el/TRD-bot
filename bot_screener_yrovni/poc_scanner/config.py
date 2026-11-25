"""Конфигурация POC-скринера: дефолты из ТЗ + переопределения по символу.

Все параметры читаются из переменных окружения (``.env`` рядом с модулем
или в корне проекта). Значения по умолчанию совпадают с ТЗ.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

# Окна POC по умолчанию: имя -> дни истории.
DEFAULT_WINDOWS: tuple[str, ...] = (
    "3d",
    "5d",
    "7d",
    "14d",
    "30d",
    "180d",
    "365d",
    "730d",
)
# Порядок важен: окна до 30 дней считаются по часовым свечам,
# 180 дней и длиннее — по четырёхчасовым (иначе 730d = 17520 свечей).
HOURLY_MAX_DAYS = 30
INTERVAL_HOURLY = "60"
INTERVAL_FOUR_HOUR = "240"
HOURS_PER_CANDLE = {INTERVAL_HOURLY: 1, INTERVAL_FOUR_HOUR: 4}

# Возраст POC по окну, в днях. Возраст уровня — это длина окна, по которому
# он посчитан, а НЕ давность записи в БД: иначе на новом символе все уровни
# «моложе суток» и MIN_POC_AGE_DAYS отбрасывает их все.
WINDOW_AGE_DAYS: dict[str, int] = {
    "3d": 3,
    "5d": 5,
    "7d": 7,
    "14d": 14,
    "30d": 30,
    "180d": 180,
    "365d": 365,
    "730d": 730,
}

# Режимы POC по историческим отрезкам: календарный месяц, блоки по 30 дней, выкл.
SEGMENT_MODES: tuple[str, ...] = ("calendar_month", "fixed_30d", "off")

# Пороги для низколиквидных/крупных инструментов: символ -> поля конфига.
PER_SYMBOL_OVERRIDES: dict[str, dict[str, float]] = {
    "BTCUSDT": {"confirm_volume_threshold": 500_000_000.0},
    "SOLUSDT": {"confirm_volume_threshold": 100_000_000.0},
    "AVAXUSDT": {"confirm_volume_threshold": 30_000_000.0},
}


def base_window_name(name: str) -> str:
    """Базовое имя окна без ранга узла: ``"30d#3"`` -> ``"30d"``."""
    return name.split("#", 1)[0]


def parse_window_days(name: str) -> int:
    """Перевести имя окна в дни: ``"3d"`` -> 3, ``"730d"`` -> 730.

    Raises:
        ValueError: если имя не в формате ``<число>d`` или число <= 0.
    """
    text = name.strip().lower()
    if not text.endswith("d") or not text[:-1].isdigit():
        raise ValueError(f"окно должно быть в формате '<дни>d', получено {name!r}")
    days = int(text[:-1])
    if days <= 0:
        raise ValueError(f"окно должно быть положительным, получено {name!r}")
    return days


def window_interval(days: int) -> str:
    """Интервал свечей для окна: до 30 дней — 1h, длиннее — 4h."""
    return INTERVAL_HOURLY if days <= HOURLY_MAX_DAYS else INTERVAL_FOUR_HOUR


def get_poc_age_days(window: str) -> int:
    """Возраст POC по окну в днях: ``"30d"`` -> 30, ``"30d#3"`` -> 30.

    Окна вне ``WINDOW_AGE_DAYS`` оцениваются по имени (``"10d"`` -> 10),
    нераспознанное имя даёт 0 — такой уровень считается слишком молодым.
    """
    name = base_window_name(window.strip().lower())
    if name in WINDOW_AGE_DAYS:
        return WINDOW_AGE_DAYS[name]
    try:
        return parse_window_days(name)
    except ValueError:
        return 0


@dataclass(frozen=True)
class WindowSpec:
    """Описание окна POC: имя, длина в днях и рабочий интервал свечей."""

    name: str
    days: int
    interval: str

    @property
    def candles(self) -> int:
        """Сколько свечей покрывают окно целиком."""
        return self.days * 24 // HOURS_PER_CANDLE[self.interval]


def window_specs(windows: tuple[str, ...] = DEFAULT_WINDOWS) -> tuple[WindowSpec, ...]:
    """Собрать описания окон, сохранив порядок из конфига."""
    specs: list[WindowSpec] = []
    for name in windows:
        days = parse_window_days(name)
        specs.append(WindowSpec(name=name, days=days, interval=window_interval(days)))
    return tuple(specs)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_path(name: str, default: str) -> Path:
    raw = os.getenv(name)
    return Path(raw.strip()) if raw and raw.strip() else Path(default)


def _env_str(name: str, default: str, allowed: tuple[str, ...] = ()) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if allowed and value not in allowed:
        return default
    return value


def _env_windows(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not items:
        return default
    for item in items:
        parse_window_days(item)  # валидация: упадёт на кривом значении
    return items


@dataclass(frozen=True)
class POCConfig:
    """Настройки POC-скринера для одного символа."""

    symbol: str = ""
    windows: tuple[str, ...] = DEFAULT_WINDOWS
    bins: int = 100
    va_percent: float = 0.68
    profile_nodes: int = 6
    node_min_share: float = 0.35
    confirm_volume_threshold: float = 50_000_000.0
    min_signal_volume: float = 10_000_000.0
    wall_volume_threshold: float = 200_000_000.0
    touch_proximity_pct: float = 0.005
    merge_poc_pct: float = 0.01
    cluster_merge_pct: float = 0.05
    zone_pct: float = 0.15
    zone_quiet_hours: float = 24.0
    zone_fresh_hours: float = 2.0
    min_departure_pct: float = 0.0
    touch_lookback_hours: float = 48.0
    price_interval: str = "5"
    min_confidence: float = 0.6
    min_poc_age_days: int = 7
    max_poc_age_days: int = 730
    db_path: Path = Path("data/poc_history.sqlite")
    history_cache_days: int = 730
    signal_endpoint: str = "http://127.0.0.1:8787/api/signal"
    signal_timeout_sec: float = 5.0
    touch_cooldown_sec: int = 3600
    signal_cooldown_sec: int = 3600
    atr_period: int = 14
    segment_mode: str = "calendar_month"
    top_n_segments: int = 12
    segment_min_volume: float = 10_000_000.0
    poc_log: Path = Path("logs/poc_levels.jsonl")
    signal_log: Path = Path("logs/poc_signals.jsonl")

    @property
    def specs(self) -> tuple[WindowSpec, ...]:
        """Описания окон в порядке конфига."""
        return window_specs(self.windows)

    @property
    def intervals(self) -> tuple[str, ...]:
        """Интервалы свечей, которые нужны этому набору окон."""
        return tuple(dict.fromkeys(spec.interval for spec in self.specs))

    def days_for_interval(self, interval: str) -> int:
        """Глубина истории для интервала: самое длинное окно на нём."""
        days = [spec.days for spec in self.specs if spec.interval == interval]
        if not days:
            return 0
        return max(max(days), 1)

    def spec(self, window: str) -> WindowSpec:
        """Описание конкретного окна.

        Raises:
            KeyError: если окна нет в конфиге.
        """
        for spec in self.specs:
            if spec.name == window:
                return spec
        raise KeyError(f"окно {window!r} не входит в POC_WINDOWS")

    @classmethod
    def from_env(
        cls,
        symbol: str = "",
        env_path: str | Path | None = None,
    ) -> POCConfig:
        """Собрать конфиг из окружения с переопределениями для символа.

        Args:
            symbol: торговая пара; для неё применяются PER_SYMBOL_OVERRIDES.
            env_path: путь к ``.env``; None — только уже загруженное окружение.
        """
        if env_path is not None:
            load_dotenv(Path(env_path), override=False)
        else:
            load_dotenv(override=False)

        config = cls(
            symbol=symbol.upper(),
            windows=_env_windows("POC_WINDOWS", DEFAULT_WINDOWS),
            bins=_env_int("POC_BINS", 100),
            va_percent=_env_float("VA_PERCENT", 0.68),
            profile_nodes=_env_int("PROFILE_NODES", 6),
            node_min_share=_env_float("NODE_MIN_SHARE", 0.35),
            confirm_volume_threshold=_env_float(
                "CONFIRM_VOLUME_THRESHOLD", 50_000_000.0
            ),
            min_signal_volume=_env_float("MIN_SIGNAL_VOLUME", 10_000_000.0),
            wall_volume_threshold=_env_float("WALL_VOLUME_THRESHOLD", 200_000_000.0),
            touch_proximity_pct=_env_float("TOUCH_PROXIMITY_PCT", 0.005),
            merge_poc_pct=_env_float("MERGE_POC_PCT", 0.01),
            cluster_merge_pct=_env_float("CLUSTER_MERGE_PCT", 0.05),
            zone_pct=_env_float("ZONE_PCT", 0.15),
            zone_quiet_hours=_env_float("ZONE_QUIET_HOURS", 24.0),
            zone_fresh_hours=_env_float("ZONE_FRESH_HOURS", 2.0),
            min_departure_pct=_env_float("MIN_DEPARTURE_PCT", 0.0),
            touch_lookback_hours=_env_float("TOUCH_LOOKBACK_HOURS", 48.0),
            price_interval=_env_str("PRICE_INTERVAL", "5", ("1", "5", "15")),
            min_confidence=_env_float("MIN_CONFIDENCE", 0.6),
            min_poc_age_days=_env_int("MIN_POC_AGE_DAYS", 7),
            max_poc_age_days=_env_int("MAX_POC_AGE_DAYS", 730),
            db_path=_env_path("POC_HISTORY_DB", "data/poc_history.sqlite"),
            history_cache_days=_env_int("HISTORY_CACHE_DAYS", 730),
            signal_endpoint=os.getenv(
                "SIGNAL_ENDPOINT", "http://127.0.0.1:8787/api/signal"
            ),
            signal_timeout_sec=_env_float("SIGNAL_TIMEOUT_SEC", 5.0),
            touch_cooldown_sec=_env_int("TOUCH_COOLDOWN_SEC", 3600),
            signal_cooldown_sec=_env_int("SIGNAL_COOLDOWN_SEC", 3600),
            atr_period=_env_int("ATR_PERIOD", 14),
            segment_mode=_env_str("SEGMENT_MODE", "calendar_month", SEGMENT_MODES),
            top_n_segments=_env_int("TOP_N_SEGMENTS", 12),
            segment_min_volume=_env_float("SEGMENT_MIN_VOLUME_USD", 10_000_000.0),
            poc_log=_env_path("POC_LEVELS_LOG", "logs/poc_levels.jsonl"),
            signal_log=_env_path("POC_SIGNALS_LOG", "logs/poc_signals.jsonl"),
        )
        return config.with_overrides()

    def with_overrides(self) -> POCConfig:
        """Применить PER_SYMBOL_OVERRIDES к конфигу символа."""
        overrides = PER_SYMBOL_OVERRIDES.get(self.symbol.upper())
        if not overrides:
            return self
        confirm = overrides.get("confirm_volume_threshold")
        if confirm is None:
            return self
        return replace(self, confirm_volume_threshold=confirm)
