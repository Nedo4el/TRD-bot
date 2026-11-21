"""Сигнал кластера уровней — то, что скринер отправляет в дашборд.

Модель «ближайший кластер»: сигнал появляется, когда цена входит в 10 %
зону ближайшего кластера POC-уровней. TP и SL считает робот по параметрам
пользователя — скринер передаёт только символ, направление, цену входа
(уровень кластера) и время.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SIGNAL_TYPE = "POC_CLUSTER"


@dataclass
class POCSignal:
    """Вход цены в зону ближайшего кластера.

    ``signal_id`` уникален для эпизода входа в зону
    (``symbol:level:direction:episode``) и служит ключом идемпотентности
    приёмника: повторная отправка того же сигнала не создаёт дубль.
    """

    ts: str
    symbol: str
    direction: str
    entry_price: float
    price: float
    distance_pct: float
    zone_pct: float
    signal_id: str
    members: list[float] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    volume: float = 0.0
    signal_type: str = SIGNAL_TYPE

    def to_dict(self) -> dict[str, Any]:
        """JSON-представление сигнала для POST /api/signal."""
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "type": self.signal_type,
            "signal_id": self.signal_id,
            "direction": self.direction,
            "entry_price": round(float(self.entry_price), 10),
            "price": round(float(self.price), 10),
            "distance_pct": round(float(self.distance_pct), 6),
            "zone_pct": round(float(self.zone_pct), 6),
            "members": [round(float(price), 10) for price in self.members],
            "windows": list(self.windows),
            "volume": round(float(self.volume), 2),
        }
