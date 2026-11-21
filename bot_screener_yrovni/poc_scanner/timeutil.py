"""Время в UTC: единый формат ISO-8601 для БД и JSONL-логов."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Текущее время в UTC с таймзоной."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Текущее время в UTC строкой вида ``2026-11-21T12:00:00Z``."""
    return to_iso(utc_now())


def to_iso(moment: datetime) -> str:
    """Привести момент к строке UTC с суффиксом ``Z`` и секундами."""
    return (
        moment.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_iso(text: str) -> datetime:
    """Разобрать ISO-8601 в aware-datetime (UTC при отсутствии таймзоны)."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)
