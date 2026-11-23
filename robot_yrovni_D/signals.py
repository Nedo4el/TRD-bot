"""Сигнал POC-скринера в терминах робота уровней.

Скринер (``bot_screener_yrovni``) шлёт сигнал кластера с полями
``signal_id / symbol / direction / entry_price / price``. Робот работает с
моделью ``POC_REACHED`` (``poc_price / side_hint / confidence / vah / val``).
Чтобы не менять скринер, парсер принимает оба контракта: отсутствующие
поля получают безопасные значения по умолчанию.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

POC_SIGNAL_TYPE = "POC_REACHED"
SCREENER_SIGNAL_TYPE = "POC_CLUSTER"
KNOWN_TYPES = frozenset({POC_SIGNAL_TYPE, SCREENER_SIGNAL_TYPE, "YROVNI_POC"})

SIDE_LONG = "long"
SIDE_SHORT = "short"


class SignalError(ValueError):
    """Сигнал не соответствует контракту — робот его не обрабатывает."""


@dataclass(frozen=True, slots=True)
class PocSignal:
    """Нормализованный сигнал POC-уровня."""

    signal_id: str
    symbol: str
    poc_price: float
    side_hint: str
    confidence: float = 1.0
    vah: float | None = None
    val: float | None = None
    current_price: float = 0.0
    ts: str = ""
    members: tuple[float, ...] = field(default_factory=tuple)
    windows: tuple[str, ...] = field(default_factory=tuple)
    volume: float = 0.0

    @property
    def side(self) -> str:
        """Сторона ордера Bybit: ``Buy`` для long, ``Sell`` для short."""
        return "Buy" if self.side_hint == SIDE_LONG else "Sell"


def _first_float(payload: Mapping[str, Any], *names: str) -> float | None:
    """Первое числовое значение из списка возможных имён полей."""
    for name in names:
        raw = payload.get(name)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _parse_side(payload: Mapping[str, Any]) -> str:
    raw = payload.get("side_hint", payload.get("direction"))
    if raw is None:
        raise SignalError("сигнал без side_hint/direction")
    side = str(raw).strip().lower()
    if side not in (SIDE_LONG, SIDE_SHORT):
        raise SignalError(f"неизвестное направление: {side!r}")
    return side


def _parse_signal_type(payload: Mapping[str, Any]) -> None:
    raw = payload.get("type")
    if raw is None or str(raw).strip() == "":
        return
    if str(raw).strip().upper() not in KNOWN_TYPES:
        raise SignalError(f"чужая схема сигнала: {raw!r}")


def parse_signal(payload: Mapping[str, Any]) -> PocSignal:
    """Собрать :class:`PocSignal` из JSON-тела сигнала.

    Поддерживаются оба контракта:

    - ``POC_REACHED``: ``poc_price``, ``side_hint``, ``confidence``,
      ``vah``, ``val``, ``current_price``;
    - ``POC_CLUSTER`` (текущий скринер): ``entry_price``, ``direction``,
      ``price``.

    Raises:
        SignalError: если сигнал не POC-типа или нет обязательных полей.
    """
    _parse_signal_type(payload)

    symbol_raw = payload.get("symbol")
    if symbol_raw is None or not str(symbol_raw).strip():
        raise SignalError("сигнал без symbol")

    poc_price = _first_float(payload, "poc_price", "entry_price")
    if poc_price is None or poc_price <= 0:
        raise SignalError("сигнал без положительного poc_price/entry_price")

    side_hint = _parse_side(payload)

    confidence = _first_float(payload, "confidence")
    if confidence is None:
        confidence = 1.0
    confidence = min(max(confidence, 0.0), 1.0)

    current_price = _first_float(payload, "current_price", "price") or 0.0
    if current_price <= 0:
        current_price = 0.0

    signal_id = str(payload.get("signal_id") or "").strip()
    if not signal_id:
        signal_id = f"{str(symbol_raw).strip().upper()}:{poc_price:.10g}:{side_hint}"

    return PocSignal(
        signal_id=signal_id,
        symbol=str(symbol_raw).strip().upper(),
        poc_price=poc_price,
        side_hint=side_hint,
        confidence=confidence,
        vah=_first_float(payload, "vah"),
        val=_first_float(payload, "val"),
        current_price=current_price,
        ts=str(payload.get("ts") or ""),
        members=tuple(float(item) for item in payload.get("members", []) or []),
        windows=tuple(str(item) for item in payload.get("windows", []) or []),
        volume=_first_float(payload, "volume") or 0.0,
    )
