"""Прогресс ручного сканирования: сколько символов проверено и сколько осталось.

Живёт в памяти процесса дашборда: планировщик ставит отметку «старт»,
адаптеры сообщают общее число символов и отмечают обработанные. UI
показывает полосу и оценку остатка до завершения.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ScanProgress:
    """Счётчик одного прогона скринера."""

    total: int = 0
    done: int = 0
    started_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Снимок для API: проценты и оценка остатка."""
        elapsed = max(0.0, time.monotonic() - self.started_at)
        percent = min(100.0, self.done / self.total * 100.0) if self.total else 0.0
        eta: float | None = None
        if self.done > 0 and self.total > self.done:
            eta = round(elapsed / self.done * (self.total - self.done), 1)
        return {
            "total": self.total,
            "done": self.done,
            "percent": round(percent, 1),
            "elapsed_sec": round(elapsed, 1),
            "eta_sec": eta,
        }


_PROGRESS: dict[str, ScanProgress] = {}


def start(name: str) -> None:
    """Начать прогон скринера (сбрасывает предыдущий прогресс)."""
    _PROGRESS[name] = ScanProgress(started_at=time.monotonic())


def set_total(name: str, total: int) -> None:
    """Задать общее число символов прогона."""
    item = _PROGRESS.get(name)
    if item is not None:
        item.total = max(0, total)


def advance(name: str, count: int = 1) -> None:
    """Отметить обработанные символы."""
    item = _PROGRESS.get(name)
    if item is not None:
        item.done += max(0, count)


def clear(name: str) -> None:
    """Убрать прогресс после завершения прогона."""
    _PROGRESS.pop(name, None)


def snapshot(name: str) -> dict[str, Any] | None:
    """Снимок прогресса или None, если скринер не сканирует."""
    item = _PROGRESS.get(name)
    return item.to_dict() if item is not None else None
