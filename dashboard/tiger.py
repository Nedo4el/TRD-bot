"""Клиент локального WebSocket API терминала Tiger Trade."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import websockets

logger = logging.getLogger(__name__)

DEFAULT_URL = "ws://127.0.0.1:7819/"
DEFAULT_TIMEOUT = 3.0

# Все скринеры дашборда работают по фьючерсам Bybit.
EXCHANGE = "BYBIT"
MARKET = "FUTURES"

MODES = ("combo", "link", "multiex")

# Где искать спот-пару для мультибиржевого стакана.
SPOT_EXCHANGES = ("BYBIT", "BINANCE")
_QUOTES = ("USDT", "USDC")

_UNREACHABLE = "Tiger не отвечает: включите локальный сервер сигналов"


def spot_symbols(symbol: str) -> list[str]:
    """Варианты спотового тикера: 1000PEPEUSDT -> PEPEUSDT."""
    code = symbol.strip().upper()
    quote = next((q for q in _QUOTES if code.endswith(q)), "")
    base = code[: -len(quote)] if quote else code
    stripped = base.lstrip("0123456789")
    if stripped and stripped != base:
        return [code, stripped + quote]
    return [code]


def build_request(
    symbol: str,
    *,
    mode: str = "link",
    link_group: str = "A",
    request_id: str | None = None,
) -> dict[str, Any]:
    """Собрать JSON-команду: группа линковки или мультибиржевой стакан."""
    payload: dict[str, Any] = {
        "type": "OPENMULTIEXSYMBOL" if mode == "multiex" else "SETLINKSYMBOL",
        "requestId": request_id or uuid.uuid4().hex,
        "e": EXCHANGE,
        "m": MARKET,
        "symbol": symbol.strip().upper(),
    }
    if mode != "multiex":
        payload["linkGroup"] = link_group
    return payload


def parse_response(raw: str) -> tuple[bool, str]:
    """Разобрать ответ Tiger API в пару (успех, сообщение)."""
    try:
        data = json.loads(raw)
    except ValueError:
        return False, "неожиданный ответ Tiger"
    if not isinstance(data, dict):
        return False, "неожиданный ответ Tiger"
    response = data.get("response")
    if not isinstance(response, dict):
        return False, "неожиданный ответ Tiger"
    if response.get("type") == "successfully":
        return True, str(data.get("message") or "готово")
    error = response.get("error")
    message = error.get("message") if isinstance(error, dict) else None
    return False, str(message or data.get("title") or "ошибка Tiger")


async def send(
    payload: dict[str, Any],
    *,
    url: str = DEFAULT_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Отправить команду в Tiger и дождаться ответа."""
    try:
        async with websockets.connect(url, open_timeout=timeout) as socket:
            await socket.send(json.dumps(payload))
            while True:
                raw = await asyncio.wait_for(socket.recv(), timeout=timeout)
                if isinstance(raw, bytes) or raw == "pong":
                    continue
                return parse_response(raw)
    except (OSError, TimeoutError, websockets.WebSocketException) as exc:
        logger.warning("Tiger API недоступен: %s", exc)
        return False, _UNREACHABLE


async def open_spot_book(
    symbol: str,
    *,
    url: str = DEFAULT_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Открыть мультибиржевой спотовый стакан (окно «All Exchanges»)."""
    for code in spot_symbols(symbol):
        for exchange in SPOT_EXCHANGES:
            payload: dict[str, Any] = {
                "type": "OPENMULTIEXSYMBOL",
                "requestId": uuid.uuid4().hex,
                "e": exchange,
                "m": "SPOT",
                "symbol": code,
            }
            ok, message = await send(payload, url=url, timeout=timeout)
            if ok:
                return True, f"спот {code} ({exchange})"
            if message == _UNREACHABLE:
                return False, message
    return False, "спота нет ни на одной бирже"


async def open_combo(
    symbol: str,
    *,
    link_group: str = "A",
    url: str = DEFAULT_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """Переключить связанные окна и открыть спотовый стакан."""
    ok, message = await send(
        build_request(symbol, mode="link", link_group=link_group),
        url=url,
        timeout=timeout,
    )
    if not ok:
        return False, message
    _, book_message = await open_spot_book(symbol, url=url, timeout=timeout)
    return True, f"график и стакан переключены, {book_message}"
