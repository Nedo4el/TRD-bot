"""Файл состояния открытой позиции robot_yrovni_D.

Формат совпадает с тем, что читает дашборд (``dashboard.positions``):
``{"symbol": ..., "position": {...}}``. Дополнительно хранится сигнал и
детали сделки (BE, тейк, трейлинг), чтобы после рестарта продолжить вести
позицию, а не бросать её.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.logger import get_logger
from robot_yrovni_D.config import LevelConfig

logger = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class StateStore:
    """Чтение и атомарная запись JSON-состояния позиции."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def for_config(cls, config: LevelConfig) -> StateStore | None:
        """Store по ``STATE_FILE``; per-symbol имя при запуске с ``SYMBOL``."""
        raw = config.state_file.strip()
        if not raw:
            return None
        path = PROJECT_ROOT / raw
        symbol = config.symbol.strip().upper()
        if symbol and not path.stem.endswith(f"_{symbol}"):
            path = path.with_name(f"{path.stem}_{symbol}{path.suffix}")
        return cls(path)

    @property
    def path(self) -> Path:
        """Файл состояния."""
        return self._path

    def load(self) -> dict[str, Any] | None:
        """Прочитать состояние; None — файла нет или он битый."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def save(self, payload: dict[str, Any]) -> None:
        """Записать состояние атомарно (tmp + replace)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(self._path)
        except OSError as exc:
            logger.warning("состояние позиции не сохранено: %s", exc)

    def clear(self, symbol: str = "") -> None:
        """Удалить файл состояния, если он про этот символ."""
        if symbol:
            current = self.load()
            current_symbol = str((current or {}).get("symbol") or "").upper()
            if current_symbol and current_symbol != symbol.upper():
                return
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("состояние позиции не удалено: %s", exc)
