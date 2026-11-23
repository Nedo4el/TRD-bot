"""Режим сканирования POC: вручную (кнопка) или авто по таймеру.

Дашборд-сканер уровней умеет работать двумя способами: скан запускается
только из UI/API либо повторяется сам через выбранный интервал. Настройка
хранится в JSON и читается планировщиком на каждом тике, поэтому режим
переключается без перезапуска сервера.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from core.logger import get_logger
from dashboard.config import DATA_DIR

logger = get_logger(__name__)

SCAN_MODE_FILE = DATA_DIR / "scan_mode.json"

MODE_MANUAL = "manual"
MODE_AUTO = "auto"
MODES: tuple[str, ...] = (MODE_MANUAL, MODE_AUTO)

# Таймеры, которые видит пользователь: 1 мин, 10 мин, 30 мин, 1 час.
INTERVALS: tuple[int, ...] = (60, 600, 1800, 3600)
DEFAULT_INTERVAL_SEC = 600

# Автоскан ведёт только скринер уровней: остальные карточки остаются ручными.
AUTO_SCREENER = "yrovni"


@dataclass(slots=True)
class ScanModeSettings:
    """Режим скана POC-скринера и интервал автоскана в секундах."""

    mode: str = MODE_MANUAL
    interval_sec: int = DEFAULT_INTERVAL_SEC

    @classmethod
    def load(cls) -> ScanModeSettings:
        """Прочитать настройку с диска; ошибка чтения — безопасный ручной режим."""
        try:
            raw = json.loads(SCAN_MODE_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls.from_dict(raw)

    def save(self) -> None:
        """Атомарно записать настройку."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = SCAN_MODE_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(asdict(self), ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(SCAN_MODE_FILE)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ScanModeSettings:
        """Собрать настройку из произвольного dict (API-вход) с валидацией."""
        defaults = cls()
        mode = str(raw.get("mode", defaults.mode)).strip().lower()
        if mode not in MODES:
            mode = defaults.mode
        try:
            interval = int(raw.get("interval_sec", defaults.interval_sec))
        except (TypeError, ValueError):
            interval = defaults.interval_sec
        if interval not in INTERVALS:
            interval = defaults.interval_sec
        return cls(mode=mode, interval_sec=interval)

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать для JSON-API."""
        return asdict(self)
