"""Сохранение состояния бота в JSON-файл.

Зачем: бот может упасть в любой момент. При перезапуске мы должны
знать, какие позиции открыты и какие ордера уже были выставлены,
чтобы не дублировать сделки.

Хранение в простом JSON — достаточно для личного бота 24/7
(без внешней БД). Доступ потокобезопасен через asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class StoredPosition:
    """Позиция в хранилище состояния."""

    symbol: str
    side: str
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float


@dataclass
class BotState:
    """Всё состояние бота, сохраняемое на диск."""

    positions: dict[str, StoredPosition] = field(default_factory=dict)
    # ID исполненных ордеров — чтобы не создавать дубли при рестарте
    processed_order_ids: list[str] = field(default_factory=list)


class StateStore:
    """Загрузка/сохранение состояния в JSON-файл."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()  # защита от одновременной записи
        self.state = BotState()
        self._load()

    def _load(self) -> None:
        """Прочитать состояние с диска (если файл существует)."""
        if not self.path.exists():
            logger.info(
                "Файл состояния %s не найден — стартуем с чистого листа", self.path
            )
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = BotState(
                positions={
                    sym: StoredPosition(**pos)
                    for sym, pos in data.get("positions", {}).items()
                },
                processed_order_ids=data.get("processed_order_ids", []),
            )
            logger.info(
                "Состояние восстановлено: %d позиция(ий), %d ордеров в истории",
                len(self.state.positions),
                len(self.state.processed_order_ids),
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Файл состояния повреждён (%s) — начинаем заново", exc)
            self.state = BotState()

    async def save(self) -> None:
        """Сохранить текущее состояние на диск (атомарная запись)."""
        async with self._lock:
            # Пишем во временный файл, затем переименовываем —
            # если процесс упадёт посреди записи, основной файл останется цел
            tmp = self.path.with_suffix(".json.tmp")
            payload = {
                "positions": {
                    sym: asdict(pos) for sym, pos in self.state.positions.items()
                },
                "processed_order_ids": self.state.processed_order_ids,
            }
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # --- Операции с позициями ---

    def get_position(self, symbol: str) -> StoredPosition | None:
        """Вернуть сохранённую позицию по символу."""
        return self.state.positions.get(symbol)

    async def set_position(self, position: StoredPosition) -> None:
        """Сохранить/обновить позицию и записать на диск."""
        self.state.positions[position.symbol] = position
        await self.save()

    async def clear_position(self, symbol: str) -> None:
        """Удалить позицию (после закрытия) и записать на диск."""
        if symbol in self.state.positions:
            del self.state.positions[symbol]
            await self.save()

    # --- Защита от дублей ордеров ---

    def is_order_processed(self, order_id: str) -> bool:
        """Проверить, обрабатывали ли мы уже этот ордер."""
        return order_id in self.state.processed_order_ids

    async def mark_order_processed(self, order_id: str) -> None:
        """Пометить ордер как обработанный (и сохранить на диск)."""
        if order_id not in self.state.processed_order_ids:
            self.state.processed_order_ids.append(order_id)
            # Ограничиваем историю последними 1000 ордеров
            if len(self.state.processed_order_ids) > 1000:
                self.state.processed_order_ids = self.state.processed_order_ids[-1000:]
            await self.save()
