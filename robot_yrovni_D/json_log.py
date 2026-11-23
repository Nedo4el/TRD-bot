"""JSONL-журнал событий стратегии.

Одна строка — одно событие: ``{"ts": ..., "action": "LIMITS_PLACED", ...}``.
Запись идёт атомарно (одна строка + flush + fsync), ротация по размеру,
старые ротированные файлы удаляются по возрасту. Любая ошибка ввода-вывода
глушится: журнал не должен ломать торговлю.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CLEANUP_INTERVAL_SEC = 3600.0


def utc_iso(moment: float | None = None) -> str:
    """Время в ISO-8601 UTC с миллисекундами."""
    stamp = datetime.fromtimestamp(
        time.time() if moment is None else moment,
        tz=timezone.utc,
    )
    return stamp.isoformat(timespec="milliseconds")


class JsonlLogger:
    """Журнал событий стратегии в формате JSONL."""

    def __init__(
        self,
        path: str | Path,
        *,
        rotation_mb: float = 50.0,
        keep_days: int = 7,
        dry_run: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._rotation_bytes = max(int(rotation_mb * 1024 * 1024), 1024)
        self._keep_seconds = max(keep_days, 0) * 86400.0
        self._dry_run = dry_run
        self._clock = clock
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    @property
    def path(self) -> Path:
        """Текущий файл журнала."""
        return self._path

    def log(self, action: str, **fields: Any) -> dict[str, Any]:
        """Дописать событие; вернуть записанную запись (даже если запись сорвалась)."""
        now = self._clock()
        record: dict[str, Any] = {"ts": utc_iso(now), "action": action}
        record.update(fields)
        if self._dry_run:
            record["dry_run"] = True

        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                self._rotate_if_needed()
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._cleanup_if_due(now)
            except OSError:
                # Журнал — вспомогательный слой: он не имеет права уронить робота.
                pass
        return record

    def _rotate_if_needed(self) -> None:
        if not self._path.exists():
            return
        if self._path.stat().st_size < self._rotation_bytes:
            return
        stamp = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        target = self._path.with_name(
            f"{self._path.stem}-{stamp:%Y%m%d-%H%M%S}{self._path.suffix}",
        )
        counter = 1
        while target.exists():
            target = self._path.with_name(
                f"{self._path.stem}-{stamp:%Y%m%d-%H%M%S}-{counter}{self._path.suffix}",
            )
            counter += 1
        self._path.rename(target)

    def _cleanup_if_due(self, now: float) -> None:
        if self._keep_seconds <= 0 or now - self._last_cleanup < CLEANUP_INTERVAL_SEC:
            return
        self._last_cleanup = now
        cutoff = now - self._keep_seconds
        pattern = f"{self._path.stem}-*{self._path.suffix}"
        for candidate in self._path.parent.glob(pattern):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink(missing_ok=True)
            except OSError:
                continue
