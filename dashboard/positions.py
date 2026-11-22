"""Открытые позиции роботов: state-файлы + сверка с биржей + ручное закрытие.

Истина об открытой позиции — биржа. Из state-файлов роботов берётся то,
чего нет на бирже: когда позиция открыта (opened_at) и уровни SL/TP.
Ручное закрытие идёт рыночным reduce-only ордером ключами того робота,
которому принадлежит монета, поэтому работает и при остановленном роботе.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.bybit_client import BybitClient, Position
from core.config import Config
from dashboard.config import PROJECT_ROOT, ScreenerEnv
from dashboard.robots import (
    get_all_robots_status,
    get_available_robots,
    get_robot_symbols,
)

logger = logging.getLogger(__name__)

# Ручные действия редкие: сериализуем, чтобы двойной клик не выдал два ордера.
_ACTION_LOCK = asyncio.Lock()
_FLAT_RETRIES = 6
_FLAT_DELAY = 0.5


@dataclass(slots=True)
class StatePosition:
    """Позиция из state-файла робота (без цен с биржи)."""

    symbol: str
    side: str
    entry_price: float
    qty: float
    stop_loss: float | None
    take_profit: float | None
    opened_at: float


def _env(robot: str) -> ScreenerEnv:
    """Настройки робота из его .env (без изменения os.environ)."""
    return ScreenerEnv(PROJECT_ROOT / robot)


def _state_files(robot: str) -> list[Path]:
    """State-файлы робота: базовый из .env и per-symbol варианты."""
    raw = _env(robot).string("STATE_FILE")
    if not raw:
        return []
    base = PROJECT_ROOT / raw
    if not base.suffix or not base.parent.is_dir():
        return []
    files = [
        path
        for path in sorted(base.parent.glob(f"{base.stem}*{base.suffix}"))
        if path.is_file() and ".bak" not in path.name and not path.name.endswith(".tmp")
    ]
    return files


def _opt_float(value: Any) -> float | None:
    """Число из JSON или None (0 и мусор — «уровня нет»)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def state_positions(robot: str) -> dict[str, StatePosition]:
    """Открытые позиции из state-файлов робота (symbol -> позиция)."""
    raw = _env(robot).string("STATE_FILE")
    if not raw:
        return {}
    base = PROJECT_ROOT / raw
    found: dict[str, StatePosition] = {}
    for path in _state_files(robot):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("state %s не прочитан: %s", path, exc)
            continue
        if not isinstance(payload, dict):
            continue
        position = payload.get("position")
        if not isinstance(position, dict):
            continue
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol and path.stem.startswith(f"{base.stem}_"):
            symbol = path.stem[len(base.stem) + 1 :].upper()
        if not symbol:
            continue
        try:
            item = StatePosition(
                symbol=symbol,
                side=(
                    "short"
                    if str(position.get("side", "long")).lower() == "short"
                    else "long"
                ),
                entry_price=float(position.get("entry_price") or 0.0),
                qty=float(position.get("qty") or 0.0),
                stop_loss=_opt_float(position.get("stop_loss")),
                take_profit=_opt_float(position.get("take_profit")),
                opened_at=float(position.get("opened_at") or 0.0),
            )
        except (TypeError, ValueError):
            continue
        previous = found.get(symbol)
        if previous is None or item.opened_at >= previous.opened_at:
            found[symbol] = item
    return found


def _client(robot: str) -> BybitClient | None:
    """Клиент Bybit ключами робота (None — ключей нет)."""
    env = _env(robot)
    api_key = env.string("BYBIT_API_KEY")
    api_secret = env.string("BYBIT_API_SECRET")
    if not api_key or not api_secret:
        return None
    return BybitClient(
        Config(
            api_key=api_key,
            api_secret=api_secret,
            testnet=env.boolean("TESTNET", False),
            category=env.string("CATEGORY", "linear") or "linear",
        )
    )


def _iso(ts: float) -> str | None:
    """Unix-время (сек) → ISO UTC; 0 — время неизвестно."""
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _row(
    robot: str,
    position: Position,
    state: StatePosition | None,
    running: bool,
) -> dict[str, Any]:
    """Строка ответа API по позиции с биржи."""
    side = "long" if position.side in ("Buy", "long") else "short"
    entry = position.avg_price or (state.entry_price if state else 0.0)
    notional = entry * position.size
    pnl_pct = position.unrealised_pnl / notional * 100.0 if notional > 0 else 0.0
    return {
        "robot": robot,
        "symbol": position.symbol,
        "side": side,
        "qty": position.size,
        "entry_price": entry,
        "mark_price": position.mark_price,
        "unrealized_pnl": position.unrealised_pnl,
        "pnl_pct": round(pnl_pct, 4),
        "stop_loss": position.stop_loss or (state.stop_loss if state else None),
        "take_profit": position.take_profit or (state.take_profit if state else None),
        "opened_at": _iso(state.opened_at) if state else None,
        "robot_running": running,
        "source": "exchange",
    }


def _state_row(robot: str, state: StatePosition, running: bool) -> dict[str, Any]:
    """Строка ответа API по позиции из state (биржа недоступна)."""
    return {
        "robot": robot,
        "symbol": state.symbol,
        "side": state.side,
        "qty": state.qty,
        "entry_price": state.entry_price,
        "mark_price": 0.0,
        "unrealized_pnl": 0.0,
        "pnl_pct": 0.0,
        "stop_loss": state.stop_loss,
        "take_profit": state.take_profit,
        "opened_at": _iso(state.opened_at),
        "robot_running": running,
        "source": "state",
    }


def _add_unconfirmed(
    rows: list[dict[str, Any]],
    notes: list[str],
    robot: str,
    states: dict[str, StatePosition],
    running: set[str],
) -> None:
    """Добавить позиции из state, когда биржа их не подтвердила.

    У работающего робота state обновляется на каждом тике — показываем с
    пометкой source=state. У остановленного state — снимок на момент
    остановки: после ручного закрытия там остаётся «позиция», которой на
    бирже уже нет. Такую строку не показываем, только поясняем.
    """
    if not states:
        return
    if robot in running:
        rows.extend(_state_row(robot, item, True) for item in states.values())
        return
    notes.append(
        f"{robot}: {', '.join(sorted(states))} — есть в state-файле, но робот "
        "остановлен и биржа недоступна; как открытые не показываем"
    )


async def list_open_positions() -> tuple[
    list[dict[str, Any]], list[dict[str, str]], list[str]
]:
    """Открытые позиции всех роботов: (строки, ошибки, пояснения).

    Позицию, которой нет на бирже, не показываем (её уже закрыли). Если
    биржа недоступна, показываем state-позиции только работающих роботов:
    state остановленного робота — устаревший снимок (фантомная строка).
    """
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    notes: list[str] = []
    running = {status.name for status in get_all_robots_status() if status.running}
    for robot in get_available_robots():
        states = state_positions(robot)
        if not states and robot not in running:
            continue
        client = _client(robot)
        if client is None:
            message = "нет API-ключей в .env робота"
            errors.append({"robot": robot, "error": message})
            _add_unconfirmed(rows, notes, robot, states, running)
            continue
        try:
            positions = await client.get_all_positions()
        except Exception as exc:  # noqa: BLE001
            message = f"биржа недоступна: {exc}"
            logger.warning("позиции %s: %s", robot, message)
            errors.append({"robot": robot, "error": message})
            _add_unconfirmed(rows, notes, robot, states, running)
            continue
        allowed = set(get_robot_symbols(robot)) | set(states)
        for position in positions:
            if position.symbol not in allowed:
                continue
            rows.append(
                _row(robot, position, states.get(position.symbol), robot in running)
            )
    rows.sort(key=lambda row: (row["robot"], row["symbol"]))
    return rows, errors, notes


async def _wait_flat(client: BybitClient, symbol: str) -> bool:
    """Дождаться, что позиции на бирже больше нет."""
    for _ in range(_FLAT_RETRIES):
        try:
            if await client.get_position(symbol) is None:
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ожидание закрытия %s: %s", symbol, exc)
        await asyncio.sleep(_FLAT_DELAY)
    return False


async def close_robot_position(
    robot: str,
    symbol: str,
    pct: float = 100.0,
) -> tuple[bool, str]:
    """Закрыть позицию робота по рынку (полностью или на pct%).

    Returns:
        (успех, сообщение для пользователя).
    """
    robot = robot.strip()
    symbol = symbol.strip().upper()
    if robot not in get_available_robots():
        return False, f"Робот {robot} не найден"
    allowed = set(get_robot_symbols(robot)) | set(state_positions(robot))
    if symbol not in allowed:
        return False, f"{symbol} не принадлежит роботу {robot}"
    client = _client(robot)
    if client is None:
        return False, f"нет API-ключей в .env робота {robot}"
    pct = 100.0 if pct >= 100 else max(1.0, pct)

    async with _ACTION_LOCK:
        try:
            positions = await client.get_positions(symbol)
        except Exception as exc:  # noqa: BLE001
            return False, f"биржа недоступна: {exc}"
        position = next((item for item in positions if item.size > 0), None)
        if position is None:
            return False, f"{symbol}: открытой позиции на бирже нет"

        qty = position.size
        if pct < 100:
            try:
                filters = await client.get_instrument_filters(symbol)
            except Exception as exc:  # noqa: BLE001
                return False, f"фильтры инструмента недоступны: {exc}"
            qty = (
                math.floor(position.size * pct / 100.0 / filters.qty_step)
                * filters.qty_step
            )
            price = position.mark_price or position.avg_price
            if qty <= 0 or qty < filters.min_qty or qty * price < filters.min_notional:
                return (
                    False,
                    (
                        "Объём для частичного закрытия ниже минимума биржи — "
                        "закройте позицию целиком"
                    ),
                )

        close_side = "Sell" if position.side == "Buy" else "Buy"
        try:
            await client.place_order(
                symbol,
                close_side,
                qty,
                "Market",
                reduce_only=True,
                position_idx=position.position_idx or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("ручное закрытие %s %s: %s", robot, symbol, exc)
            return False, f"Ордер не выставлен: {exc}"
        logger.info(
            "ручное закрытие: %s %s %s qty=%s",
            robot,
            symbol,
            "весь объём" if pct >= 100 else f"{pct:g}%",
            qty,
        )

        if pct < 100:
            return True, f"{symbol}: закрыто {pct:g}% ({qty:g})"
        if await _wait_flat(client, symbol):
            try:
                await client.cancel_all_orders(symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "отмена заявок после закрытия %s: %s",
                    symbol,
                    exc,
                )
            return True, f"{symbol}: позиция закрыта по рынку"
        return True, f"{symbol}: ордер на закрытие отправлен (биржа ещё не подтвердила)"
