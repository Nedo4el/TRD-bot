"""Сигнал POC_REACHED — то, что скринер отправляет роботу."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SIGNAL_TYPE = "POC_REACHED"
ACTION_PLACE = "PLACE_LIMIT_ORDERS"
ACTION_OBSERVE = "OBSERVE"


@dataclass
class POCSignal:
    """Подход цены к подтверждённому POC-уровню.

    ``action`` равен ``PLACE_LIMIT_ORDERS`` только при
    ``confidence >= MIN_CONFIDENCE``; иначе ``OBSERVE`` — робот не торгует,
    но уровень виден в журнале.
    """

    ts: str
    symbol: str
    poc_price: float
    poc_windows: list[str] = field(default_factory=list)
    poc_age_days: int = 0
    total_volume_at_level: float = 0.0
    touch_count: int = 0
    is_wall: bool = False
    proximity_pct: float = 0.0
    vah: float = 0.0
    val: float = 0.0
    confidence: float = 0.0
    action: str = ACTION_PLACE
    side_hint: str = "both"
    order_params: dict[str, float] = field(default_factory=dict)
    signal_type: str = SIGNAL_TYPE

    def to_dict(self) -> dict[str, Any]:
        """JSON-представление сигнала (см. ТЗ, раздел «Формат сигнала»)."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "type": self.signal_type,
            "poc_price": round(float(self.poc_price), 10),
            "poc_windows": list(self.poc_windows),
            "poc_age_days": int(self.poc_age_days),
            "total_volume_at_level": round(float(self.total_volume_at_level), 2),
            "touch_count": int(self.touch_count),
            "is_wall": bool(self.is_wall),
            "proximity_pct": round(float(self.proximity_pct), 6),
            "vah": round(float(self.vah), 10),
            "val": round(float(self.val), 10),
            "confidence": round(float(self.confidence), 4),
            "action": self.action,
            "side_hint": self.side_hint,
            "order_params": {
                key: round(float(value), 10) for key, value in self.order_params.items()
            },
        }
