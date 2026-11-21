"""Приёмник POC-сигналов и тумблер AUTO_TRADE для дашборда.

Скринер отправляет сигнал на ``POST /api/signal``. Дашборд хранит последние
сигналы в JSON и показывает их двумя списками: «ожидает подтверждения»
(``AUTO_TRADE=false``) и «в работе» (подтверждён пользователем или
``AUTO_TRADE=true``). Флаг AUTO_TRADE читается на каждый запрос, поэтому
переключается без перезапуска сервера.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from core.logger import get_logger
from dashboard.config import DATA_DIR
from dashboard.store import now_iso

logger = get_logger(__name__)

POC_SIGNALS_FILE = DATA_DIR / "poc_signals.json"
AUTO_TRADE_FILE = DATA_DIR / "auto_trade.json"
MAX_RECORDS = 200
STATE_PENDING = "pending"
STATE_ACTIVE = "active"


@dataclass(slots=True)
class AutoTradeFlag:
    """Режим торговли: false — ждать подтверждения, true — торговать сразу."""

    auto_trade: bool = False

    @classmethod
    def load(cls) -> AutoTradeFlag:
        """Прочитать флаг с диска; ошибка чтения — безопасный false."""
        try:
            raw = json.loads(AUTO_TRADE_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return cls()
        return cls(auto_trade=bool(raw.get("auto_trade", False)))

    def save(self) -> None:
        """Атомарно записать флаг."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = AUTO_TRADE_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"auto_trade": self.auto_trade}, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(AUTO_TRADE_FILE)


@dataclass(slots=True)
class PocSignalRecord:
    """Принятый сигнал скринера и его состояние в дашборде."""

    signal_id: str
    symbol: str
    direction: str
    entry_price: float
    price: float
    distance_pct: float
    zone_pct: float
    ts: str
    received_at: str
    state: str
    members: list[float] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    volume: float = 0.0
    confirmed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать запись для JSON-API."""
        return asdict(self)


class PocSignalStore:
    """Последние POC-сигналы: дедуп по ``signal_id``, состояния pending/active."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._records: list[PocSignalRecord] = []
        self._load()

    def add(self, payload: dict[str, Any]) -> tuple[PocSignalRecord, bool]:
        """Принять сигнал; вернуть запись и признак «создан новый»."""
        signal_id = str(payload.get("signal_id") or "")
        existing = self._find(signal_id)
        if existing is not None:
            return existing, False
        auto = AutoTradeFlag.load().auto_trade
        record = PocSignalRecord(
            signal_id=signal_id,
            symbol=str(payload["symbol"]).upper(),
            direction=str(payload["direction"]).lower(),
            entry_price=float(payload["entry_price"]),
            price=float(payload.get("price", 0.0)),
            distance_pct=float(payload.get("distance_pct", 0.0)),
            zone_pct=float(payload.get("zone_pct", 0.10)),
            ts=str(payload.get("ts", "")),
            received_at=now_iso(),
            state=STATE_ACTIVE if auto else STATE_PENDING,
            members=[float(price) for price in payload.get("members", [])],
            windows=[str(name) for name in payload.get("windows", [])],
            volume=float(payload.get("volume", 0.0)),
        )
        self._records.insert(0, record)
        self._records = self._records[:MAX_RECORDS]
        self._persist()
        logger.info(
            "POC-сигнал %s: %s %s @ %.10g (%s)",
            record.signal_id,
            record.symbol,
            record.direction,
            record.entry_price,
            record.state,
        )
        return record, True

    def confirm(self, signal_id: str) -> PocSignalRecord | None:
        """Подтвердить сигнал: pending -> active (идемпотентно)."""
        record = self._find(signal_id)
        if record is None:
            return None
        if record.state == STATE_PENDING:
            record.state = STATE_ACTIVE
            record.confirmed_at = now_iso()
            self._persist()
            logger.info("POC-сигнал %s подтверждён", signal_id)
        return record

    def snapshot(self) -> dict[str, Any]:
        """Снимок для дашборда: флаг режима, счётчики и список сигналов."""
        pending = sum(1 for item in self._records if item.state == STATE_PENDING)
        active = sum(1 for item in self._records if item.state == STATE_ACTIVE)
        return {
            "generated_at": now_iso(),
            "auto_trade": AutoTradeFlag.load().auto_trade,
            "pending": pending,
            "active": active,
            "signals": [item.to_dict() for item in self._records],
        }

    def _find(self, signal_id: str) -> PocSignalRecord | None:
        return next(
            (item for item in self._records if item.signal_id == signal_id),
            None,
        )

    def _persist(self) -> None:
        """Атомарно записать список сигналов на диск."""
        temp = POC_SIGNALS_FILE.with_suffix(".tmp")
        try:
            temp.write_text(
                json.dumps(
                    {"signals": [item.to_dict() for item in self._records]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temp.replace(POC_SIGNALS_FILE)
        except OSError as exc:
            logger.warning("Не удалось сохранить POC-сигналы: %s", exc)

    def _load(self) -> None:
        """Восстановить сигналы после рестарта дашборда."""
        if not POC_SIGNALS_FILE.exists():
            return
        try:
            payload = json.loads(POC_SIGNALS_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Не удалось прочитать POC-сигналы: %s", exc)
            return
        restored: list[PocSignalRecord] = []
        for item in payload.get("signals", []):
            if not isinstance(item, dict):
                continue
            try:
                restored.append(PocSignalRecord(**item))
            except TypeError:
                continue
        self._records = restored[:MAX_RECORDS]
