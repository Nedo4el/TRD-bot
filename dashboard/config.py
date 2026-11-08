"""Конфигурация дашборда и реестр скринеров."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data" / "dashboard"
STATE_FILE = DATA_DIR / "latest.json"

# Минимальный интервал сканирования — защита от лимитов Bybit.
MIN_INTERVAL_SEC = 30


@dataclass(frozen=True, slots=True)
class ScreenerSpec:
    """Описание одного скринера."""

    name: str
    title: str
    dir_name: str
    interval_sec: int
    max_symbols: int
    heavy: bool = False


SCREENERS: tuple[ScreenerSpec, ...] = (
    ScreenerSpec("impulse", "Импульс", "bot_screener_impulse", 60, 60),
    ScreenerSpec("klin", "Сжатие (клин)", "bot_screener_klin", 180, 40),
    ScreenerSpec("uzkiy", "Узкий диапазон", "bot_screener_uzkiy", 180, 60),
    ScreenerSpec("krugloe", "Круглые числа", "bot_screener_krugloe", 120, 60),
    ScreenerSpec("trend", "Тренд HH/HL", "bot_screener_trend", 180, 40),
    ScreenerSpec("pump", "Накопление", "bot_screener_pump", 600, 40, True),
    ScreenerSpec("yrovni", "Volume Profile", "bot_screener_yrovni", 300, 25, True),
    ScreenerSpec(
        "zakonomer",
        "Закономерность",
        "bot_screener_zakonomer",
        1800,
        12,
        True,
    ),
    ScreenerSpec("all", "Все монеты", "all", 60, 100),
)

SCREENERS_BY_NAME = {spec.name: spec for spec in SCREENERS}
ALL_SCREENERS = tuple(spec.name for spec in SCREENERS)


class ScreenerEnv:
    """Чтение .env конкретного скринера без изменения os.environ."""

    def __init__(self, bot_dir: Path) -> None:
        raw = dotenv_values(bot_dir / ".env")
        self._values = {k: v for k, v in raw.items() if v is not None}
        self._bot_dir = bot_dir

    def string(self, key: str, default: str = "") -> str:
        """Строковое значение."""
        return str(self._values.get(key, default)).strip()

    def integer(self, key: str, default: int) -> int:
        """Целочисленное значение."""
        try:
            return int(float(self.string(key, str(default))))
        except ValueError:
            return default

    def number(self, key: str, default: float) -> float:
        """Число с плавающей точкой."""
        try:
            return float(self.string(key, str(default)))
        except ValueError:
            return default

    def boolean(self, key: str, default: bool) -> bool:
        """Логическое значение."""
        value = self.string(key, "true" if default else "false").lower()
        return value in ("1", "true", "yes", "on")

    def names(self, key: str, default: tuple[str, ...] = ()) -> list[str]:
        """Список строк через запятую."""
        raw = self.string(key)
        if not raw:
            return list(default)
        return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(slots=True)
class DashboardConfig:
    """Настройки HTTP-сервера и планировщика."""

    host: str = "127.0.0.1"
    port: int = 8787
    concurrency: int = 1
    max_symbols: int | None = None
    enabled: tuple[str, ...] = ALL_SCREENERS
    open_browser: bool = False
    tiger_url: str = "ws://127.0.0.1:7819/"
    tiger_mode: str = "combo"
    tiger_link_group: str = "A"

    @classmethod
    def from_env(cls) -> DashboardConfig:
        """Считать настройки из переменных окружения."""
        raw_enabled = os.getenv("DASH_ENABLED", "").strip()
        enabled = (
            tuple(name.strip() for name in raw_enabled.split(",") if name.strip())
            if raw_enabled
            else ALL_SCREENERS
        )
        raw_max = os.getenv("DASH_MAX_SYMBOLS", "").strip()
        mode = os.getenv("TIGER_OPEN_MODE", "combo").strip().lower()
        return cls(
            host=os.getenv("DASH_HOST", "127.0.0.1"),
            port=int(os.getenv("DASH_PORT", "8787")),
            concurrency=max(1, int(os.getenv("DASH_CONCURRENCY", "1"))),
            max_symbols=int(raw_max) if raw_max else None,
            enabled=enabled,
            open_browser=os.getenv("DASH_OPEN_BROWSER", "").strip().lower()
            in ("1", "true", "yes", "on"),
            tiger_url=os.getenv("TIGER_API_URL", "ws://127.0.0.1:7819/"),
            tiger_mode=mode if mode in ("combo", "link", "multiex") else "combo",
            tiger_link_group=os.getenv("TIGER_LINK_GROUP", "A").strip().upper() or "A",
        )

    def resolved_specs(self) -> list[ScreenerSpec]:
        """Вернуть включённые скринеры с учётом их .env и общих настроек."""
        specs: list[ScreenerSpec] = []
        for spec in SCREENERS:
            if spec.name not in self.enabled:
                continue
            env = ScreenerEnv(PROJECT_ROOT / spec.dir_name)
            interval = max(
                MIN_INTERVAL_SEC,
                env.integer("SCAN_INTERVAL", spec.interval_sec),
            )
            max_symbols = spec.max_symbols
            if self.max_symbols is not None:
                max_symbols = min(max_symbols, self.max_symbols)
            specs.append(
                replace(
                    spec,
                    interval_sec=interval,
                    max_symbols=max(1, max_symbols),
                )
            )
        return specs
