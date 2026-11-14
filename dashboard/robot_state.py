"""Состояние тренд-робота: JSON-хранилище + dataclass.

Хранит флаг запуска и выбранные пользователем символы.
Реальной биржи нет — только приём выбора и управление флагом.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from dashboard.config import DATA_DIR

ROBOT_STATE_FILE = DATA_DIR / "robot_state.json"


@dataclass(slots=True)
class RobotState:
    """Выбор пользователя: какие инструменты торгует робот."""

    running: bool = False
    symbols: list[str] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    stopped_at: str | None = None

    @classmethod
    def load(cls) -> RobotState:
        """Прочитать JSON, при ошибке — дефолты."""
        try:
            raw = json.loads(ROBOT_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls.from_dict(raw)

    def save(self) -> None:
        """Атомарно записать JSON."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = ROBOT_STATE_FILE.with_suffix(".tmp")
        payload = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        temp.write_text(payload, encoding="utf-8")
        temp.replace(ROBOT_STATE_FILE)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RobotState:
        """Собрать состояние из произвольного dict (API-вход)."""
        symbols_raw = raw.get("symbols", [])
        symbols = [str(s).upper() for s in symbols_raw if str(s).strip()]
        # Дедупликация с сохранением порядка
        symbols = list(dict.fromkeys(symbols))
        signals_raw = raw.get("signals", [])
        signals = [s for s in signals_raw if isinstance(s, dict)]
        return cls(
            running=bool(raw.get("running", False)),
            symbols=symbols,
            signals=signals,
            started_at=raw.get("started_at"),
            stopped_at=raw.get("stopped_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать для JSON-API."""
        return asdict(self)
