"""Модели данных дашборда."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Signal:
    """Нормализованный сигнал скринера."""

    screener: str
    title: str
    symbol: str
    side: str
    price: float
    score: float | None
    label: str
    turnover_24h: float
    found_at: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать сигнал в dict для JSON-API."""
        return asdict(self)


def make_signal(
    *,
    screener: str,
    title: str,
    symbol: str,
    side: str = "NEUTRAL",
    price: float = 0.0,
    score: float | None = None,
    label: str = "",
    turnover_24h: float = 0.0,
    found_at: str = "",
    detail: dict[str, Any] | None = None,
) -> Signal:
    """Собрать сигнал с безопасными значениями по умолчанию."""
    return Signal(
        screener=screener,
        title=title,
        symbol=symbol,
        side=side.upper() if side else "NEUTRAL",
        price=float(price or 0.0),
        score=None if score is None else float(score),
        label=label,
        turnover_24h=float(turnover_24h or 0.0),
        found_at=found_at,
        detail=detail or {},
    )
