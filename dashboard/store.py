"""Хранилище последних сигналов и статусов скринеров."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from core.logger import get_logger
from dashboard.config import DATA_DIR, STATE_FILE, ScreenerSpec
from dashboard.models import Signal

logger = get_logger(__name__)


def now_iso() -> str:
    """Текущее время в UTC (ISO-8601)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ScreenerStatus:
    """Состояние одного скринера."""

    name: str
    title: str
    interval_sec: int
    running: bool = False
    last_run_at: str | None = None
    duration_sec: float | None = None
    next_run_at: str | None = None
    count: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать статус в dict."""
        return asdict(self)


class SignalStore:
    """Потокобезопасное (в рамках event loop) хранилище."""

    def __init__(self, specs: list[ScreenerSpec]) -> None:
        self._specs = {spec.name: spec for spec in specs}
        self._signals: dict[str, list[Signal]] = {spec.name: [] for spec in specs}
        self._status = {
            spec.name: ScreenerStatus(
                name=spec.name,
                title=spec.title,
                interval_sec=spec.interval_sec,
            )
            for spec in specs
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def mark_running(self, name: str) -> None:
        """Пометить скринер как «сканирует»."""
        status = self._status.get(name)
        if status is not None:
            status.running = True

    def set_result(
        self,
        name: str,
        signals: list[Signal],
        duration_sec: float,
        next_run_at: str,
    ) -> None:
        """Сохранить результаты успешного прогона."""
        unique: dict[str, Signal] = {}
        for signal in signals:
            previous = unique.get(signal.symbol)
            if previous is None or (signal.score or 0) > (previous.score or 0):
                unique[signal.symbol] = signal
        ordered = sorted(
            unique.values(),
            key=lambda signal: signal.score or 0,
            reverse=True,
        )
        self._signals[name] = ordered
        status = self._status.get(name)
        if status is not None:
            status.running = False
            status.last_run_at = now_iso()
            status.duration_sec = round(duration_sec, 1)
            status.next_run_at = next_run_at
            status.count = len(ordered)
            status.error = None
        self._persist()

    def set_error(
        self,
        name: str,
        error: str,
        duration_sec: float,
        next_run_at: str,
    ) -> None:
        """Зафиксировать ошибку прогона."""
        status = self._status.get(name)
        if status is not None:
            status.running = False
            status.last_run_at = now_iso()
            status.duration_sec = round(duration_sec, 1)
            status.next_run_at = next_run_at
            status.error = error[:500]
        self._persist()

    def snapshot(self) -> dict[str, Any]:
        """Собрать снимок для API."""
        signals = [
            signal.to_dict()
            for name in self._specs
            for signal in self._signals.get(name, [])
        ]
        statuses = [status.to_dict() for status in self._status.values()]
        errors = sum(1 for status in self._status.values() if status.error)
        return {
            "generated_at": now_iso(),
            "total_signals": len(signals),
            "screeners_total": len(statuses),
            "screeners_with_errors": errors,
            "signals": signals,
            "screeners": statuses,
        }

    def _payload(self) -> dict[str, Any]:
        return {
            "updated_at": now_iso(),
            "signals": {
                name: [signal.to_dict() for signal in signals]
                for name, signals in self._signals.items()
            },
            "screeners": {
                name: status.to_dict() for name, status in self._status.items()
            },
        }

    def _persist(self) -> None:
        """Атомарно записать снимок на диск."""
        temp = STATE_FILE.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps(self._payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(STATE_FILE)
        except OSError as exc:
            logger.warning("Не удалось сохранить снимок дашборда: %s", exc)

    def _load(self) -> None:
        """Восстановить последний снимок после рестарта."""
        if not STATE_FILE.exists():
            return
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать снимок дашборда: %s", exc)
            return
        for name, items in payload.get("signals", {}).items():
            if name not in self._signals:
                continue
            restored: list[Signal] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    restored.append(Signal(**item))
                except TypeError:
                    continue
            self._signals[name] = restored
        for name, item in payload.get("screeners", {}).items():
            status = self._status.get(name)
            if status is None or not isinstance(item, dict):
                continue
            status.last_run_at = item.get("last_run_at")
            status.duration_sec = item.get("duration_sec")
            status.next_run_at = item.get("next_run_at")
            status.count = int(item.get("count", 0) or 0)
            status.error = item.get("error")
            status.running = False
