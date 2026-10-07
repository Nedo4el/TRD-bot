"""JSON-состояние robot_flat."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

PHASE_IDLE = "IDLE"
PHASE_IN_POSITION = "IN_POSITION"
PHASE_STOPPED = "STOPPED"
# версия схемы state.json: старше кода — не запускаемся, младше — мигрируем
SCHEMA_VERSION = 1


@dataclass
class OpenPosition:
    """Открытая позиция (long или short)."""

    entry_price: float
    qty: float
    side: str = "long"
    stop_loss: float | None = None
    take_profit: float | None = None
    opened_at: float = 0.0
    # BE/trail: пик цены с момента входа и состояние безубытка
    peak_price: float = 0.0
    be_active: bool = False


@dataclass
class FlatState:
    """Полное состояние бота для JSON."""

    phase: str = PHASE_IDLE
    position: OpenPosition | None = None
    session_pnl: float = 0.0
    peak_session_pnl: float = 0.0
    kill: bool = False
    # COOLDOWN_SEC: время последнего закрытия позиции (unix, с; 0 = не было)
    last_close_at: float = 0.0
    # дневной стоп-кран: счётчики текущих суток (UTC)
    day: str = ""  # "YYYY-MM-DD" по UTC
    day_pnl: float = 0.0
    day_trades: int = 0
    symbol: str = ""  # инструмент, которому принадлежит state
    schema_version: int = SCHEMA_VERSION


class StateStore:
    """Атомарный JSON-стор для FlatState."""

    def __init__(self, path: Path, symbol: str = "") -> None:
        self.path = path
        self.symbol = symbol
        self._lock = asyncio.Lock()
        self.state = FlatState()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.info("state %s не найден — чистый старт", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            position = raw.get("position")
            self.state = FlatState(
                phase=raw.get("phase", PHASE_IDLE),
                position=OpenPosition(**position) if position else None,
                session_pnl=float(raw.get("session_pnl", 0.0)),
                peak_session_pnl=float(raw.get("peak_session_pnl", 0.0)),
                kill=bool(raw.get("kill", False)),
                last_close_at=float(raw.get("last_close_at", 0.0)),
                day=str(raw.get("day", "")),
                day_pnl=float(raw.get("day_pnl", 0.0)),
                day_trades=int(raw.get("day_trades", 0)),
                symbol=str(raw.get("symbol", "")),
                schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
            )
            logger.info("state восстановлен: phase=%s", self.state.phase)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("state повреждён (%s) — с нуля", exc)
            self.state = FlatState()
        self.check_symbol()

    def check_symbol(self) -> None:
        """Отказаться работать, если state принадлежит другому инструменту."""
        stored = self.state.symbol
        if stored and self.symbol and stored != self.symbol:
            raise ValueError(
                f"state от другого инструмента: stored={stored} "
                f"config={self.symbol} ({self.path})"
            )
        if self.state.schema_version > SCHEMA_VERSION:
            raise ValueError(
                f"state новее кода: schema={self.state.schema_version} "
                f"code={SCHEMA_VERSION} ({self.path})"
            )

    async def save(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.symbol:
                self.state.symbol = self.symbol
            self.state.schema_version = SCHEMA_VERSION
            tmp = self.path.with_suffix(".tmp")
            payload = asdict(self.state)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def snapshot(self) -> dict[str, object]:
        return asdict(self.state)
