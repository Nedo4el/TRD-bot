"""JSON-состояние state machine robot_zakol."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PHASE_IDLE = "IDLE"
PHASE_WORKING = "WORKING"
PHASE_IN_POSITION = "IN_POSITION"
PHASE_STOPPED = "STOPPED"


@dataclass
class PendingOrder:
    """Лимитка, выставленная в цикле TTL."""

    order_id: str
    order_link_id: str
    price: float
    qty: float
    placed_at: float
    filled_qty: float = 0.0
    side: str = "Buy"  # Buy = вход в лонг, Sell = вход в шорт


@dataclass
class OpenPosition:
    """Открытая позиция (long или short)."""

    entry_price: float
    qty: float
    peak_price: float
    stop_loss: float
    side: str = "long"
    be_active: bool = False
    trail_stop: float | None = None
    opened_at: float = 0.0


@dataclass
class ZakolState:
    """Полное состояние бота для JSON."""

    phase: str = PHASE_IDLE
    pending_buy: PendingOrder | None = None
    pending_sell: PendingOrder | None = None
    position: OpenPosition | None = None
    session_pnl: float = 0.0
    peak_session_pnl: float = 0.0
    kill: bool = False


class StateStore:
    """Атомарный JSON-стор для ZakolState."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self.state = ZakolState()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.info("state %s не найден — чистый старт", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            pending_buy = raw.get("pending_buy")
            pending_sell = raw.get("pending_sell")
            position = raw.get("position")
            self.state = ZakolState(
                phase=raw.get("phase", PHASE_IDLE),
                pending_buy=PendingOrder(**pending_buy) if pending_buy else None,
                pending_sell=PendingOrder(**pending_sell) if pending_sell else None,
                position=OpenPosition(**position) if position else None,
                session_pnl=float(raw.get("session_pnl", 0.0)),
                peak_session_pnl=float(raw.get("peak_session_pnl", 0.0)),
                kill=bool(raw.get("kill", False)),
            )
            logger.info("state восстановлен: phase=%s", self.state.phase)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("state повреждён (%s) — с нуля", exc)
            self.state = ZakolState()

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = asdict(self.state)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def snapshot(self) -> dict[str, object]:
        return asdict(self.state)
