"""Хранилище списков торгуемых монет по роботам (JSON).

Каждый робот торгует свой набор монет. Список лежит в
``data/dashboard/robot_coins.json``. Если робота в файле нет — берётся
``SYMBOL`` из его ``.env`` (старый сценарий «одна монета на робота»).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from dashboard.config import DATA_DIR, PROJECT_ROOT

logger = logging.getLogger(__name__)

COINS_FILE = DATA_DIR / "robot_coins.json"

# Максимум монет на одного робота (защита от случайного «выбрать всё»).
MAX_SYMBOLS_PER_ROBOT = 20


def normalize_symbols(symbols: list[Any]) -> list[str]:
    """Привести тикеры к виду ``[BTCUSDT, ETHUSDT]`` (без дублей)."""
    out: list[str] = []
    for raw in symbols:
        symbol = "".join(str(raw).split()).upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def symbol_from_env(robot_name: str) -> str:
    """``SYMBOL`` из ``.env`` робота (пусто, если нет ключа или файла)."""
    env_file = PROJECT_ROOT / robot_name / ".env"
    if not env_file.exists():
        return ""
    try:
        raw = dotenv_values(env_file)
    except OSError:
        return ""
    return str(raw.get("SYMBOL") or "").strip().upper()


def _read_coins_json(path: Path) -> Any:
    """Разобрать JSON монет: UTF-8 (в т.ч. с BOM) или UTF-16.

    Файл правят и руками — PowerShell сохраняет UTF-16. Без фолбэка
    такой файл валил весь ``/api/robots`` (UnicodeDecodeError).
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"{path.name}: не UTF-8 и не UTF-16 JSON")


@dataclass(slots=True)
class RobotCoins:
    """Списки торгуемых монет по роботам."""

    robots: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> RobotCoins:
        """Прочитать JSON, при ошибке — пустое состояние."""
        try:
            raw = _read_coins_json(COINS_FILE)
        except (OSError, ValueError) as exc:
            if COINS_FILE.exists():
                logger.warning("Список монет не прочитан: %s", exc)
            return cls()
        robots: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if isinstance(value, list):
                    robots[str(name)] = normalize_symbols(value)
        return cls(robots=robots)

    def save(self) -> None:
        """Атомарно записать JSON."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = COINS_FILE.with_suffix(".tmp")
        payload = json.dumps(self.robots, ensure_ascii=False, indent=2)
        temp.write_text(payload, encoding="utf-8")
        temp.replace(COINS_FILE)

    def symbols_for(self, robot_name: str) -> list[str]:
        """Монеты робота: из файла, иначе сид из ``.env``."""
        stored = self.robots.get(robot_name)
        if stored is not None:
            return list(stored)
        seed = symbol_from_env(robot_name)
        return [seed] if seed else []

    def set(self, robot_name: str, symbols: list[Any]) -> list[str]:
        """Записать список монет робота (нормализует и ограничивает)."""
        normalized = normalize_symbols(symbols)[:MAX_SYMBOLS_PER_ROBOT]
        self.robots[robot_name] = normalized
        self.save()
        return normalized
