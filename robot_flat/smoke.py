"""Щадящая проверка живого контура Bybit перед запуском бота.

Проверяет: выставление/снятие лимиток, чтение позиции, установку серверных
SL/TP, перенос стопа на безубыток, закрытие позиции.

Шаги:
  1. время/фильтры/режим позиций/цена/баланс;
  2. лимитка далеко от рынка → видна среди открытых → снимается;
  3. ``--fill`` — минимальная лимитка-исполнение → позиция → SL/TP →
     перенос SL на вход (безубыток) → закрытие позиции.

finally: снимает все заявки символа; если позиция открыта нашим ``--fill``
и её не было на старте — закрывает её.

Запуск (бот должен быть остановлен)::

    python -m robot_flat.smoke          # без сделок: только лимитки
    python -m robot_flat.smoke --fill   # + минимальная позиция и стопы

Требует ключи в ``robot_flat/.env``. ``TESTNET=true`` — тестовая сеть.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import secrets
import sys

from core.bybit_client import BybitClient, Position
from robot_flat.config import load_config
from robot_flat.main import _core_config

logger = logging.getLogger("smoke")

POLL_TRIES = 10
POLL_DELAY = 0.5


class SmokeFail(Exception):
    """Аварийный останов шага проверки."""


def _ok(msg: str) -> None:
    logger.info("OK   %s", msg)


def _close(a: float, b: float, tick: float) -> bool:
    return abs(a - b) <= tick * 1.01


def _qty_for(
    price: float, qty_step: float, min_qty: float, min_notional: float
) -> float:
    """Количество, кратное qty_step, не меньше min_qty и min_notional."""
    need = math.ceil(min_notional / price / qty_step - 1e-9) * qty_step
    return round(max(need, min_qty), 9)


async def _wait_position(client: BybitClient, symbol: str) -> Position | None:
    for _ in range(POLL_TRIES):
        pos = await client.get_position(symbol)
        if pos is not None:
            return pos
        await asyncio.sleep(POLL_DELAY)
    return None


async def _wait_flat(client: BybitClient, symbol: str) -> bool:
    for _ in range(POLL_TRIES):
        if await client.get_position(symbol) is None:
            return True
        await asyncio.sleep(POLL_DELAY)
    return False


async def run(fill: bool) -> None:
    cfg = load_config()
    core = _core_config(cfg)
    logger.info(
        "SMOKE symbol=%s testnet=%s fill=%s",
        cfg.symbol,
        core.testnet,
        fill,
    )
    if not core.testnet:
        logger.warning("РЕЖИМ: MAINNET — сделки на реальном счете")

    client = BybitClient(core)
    opened_by_us = False
    try:
        await client.sync_time()
        f = await client.get_instrument_filters(cfg.symbol)
        mode = await client.get_position_mode(cfg.symbol)
        price = await client.get_price(cfg.symbol)
        balance = await client.get_balance()
        _ok(
            f"фильтры tick={f.tick_size} qty_step={f.qty_step} "
            f"min_qty={f.min_qty} min_notional={f.min_notional}; "
            f"режим={mode}; цена={price:g}; баланс={balance:.2f}"
        )
        if mode != "one-way":
            raise SmokeFail(f"позиционный режим {mode!r} — нужен one-way")

        pos0 = await client.get_position(cfg.symbol)
        if pos0 is not None:
            logger.warning(
                "позиция уже открыта (%s qty=%s) — не трогаю", pos0.side, pos0.size
            )
        stray = await client.get_open_orders(cfg.symbol)
        if stray:
            logger.warning("открытых заявок уже %d — сниму в finally", len(stray))

        # --- шаг 2: лимитка далеко от рынка ---
        far_price = price * 0.95
        qty = _qty_for(far_price, f.qty_step, f.min_qty, f.min_notional)
        resp = await client.place_order(
            cfg.symbol,
            "Buy",
            qty,
            "Limit",
            price=far_price,
            order_link_id=f"{cfg.order_link_prefix}smoke-far-{secrets.token_hex(4)}",
        )
        oid = str((resp.get("result") or {}).get("orderId") or "")
        if not oid:
            raise SmokeFail("биржа не вернула orderId")
        orders = await client.get_open_orders(cfg.symbol)
        if not any(o.get("orderId") == oid for o in orders):
            raise SmokeFail("лимитка не найдена среди открытых ордеров")
        _ok(f"лимитка выставлена: Buy {qty:g} @ {far_price:g} (id={oid})")

        await client.cancel_order(cfg.symbol, oid)
        orders = await client.get_open_orders(cfg.symbol)
        if any(o.get("orderId") == oid for o in orders):
            raise SmokeFail("лимитка не снялась")
        hist = await client.get_order(cfg.symbol, oid)
        status = (hist or {}).get("orderStatus", "?")
        _ok(f"лимитка снята (orderStatus={status})")

        # --- шаг 3: полный цикл с позицией ---
        if not fill:
            logger.info("--fill не передан — позицию/стопы не проверяем")
            return
        if pos0 is not None:
            logger.warning("позиция существовала до smoke — fill-шаг пропущен")
            return

        entry_ref = await client.get_price(cfg.symbol)
        fill_price = entry_ref * 1.005  # маркетабельная лимитка — гарантированный филл
        qty = _qty_for(fill_price, f.qty_step, f.min_qty, f.min_notional)
        resp = await client.place_order(
            cfg.symbol,
            "Buy",
            qty,
            "Limit",
            price=fill_price,
            order_link_id=f"{cfg.order_link_prefix}smoke-fill-{secrets.token_hex(4)}",
        )
        oid2 = str((resp.get("result") or {}).get("orderId") or "")
        pos = await _wait_position(client, cfg.symbol)
        if pos is None:
            raise SmokeFail(f"позиция не появилась (orderStatus id={oid2})")
        opened_by_us = True
        entry = pos.avg_price or entry_ref
        _ok(f"позиция открыта: {pos.side} qty={pos.size:g} entry={entry:g}")

        sl, tp = entry * 0.95, entry * 1.03
        await client.set_stop_loss_take_profit(cfg.symbol, sl, tp)
        got_sl, got_tp = await client.get_position_stops(cfg.symbol)
        if not _close(got_sl, sl, f.tick_size):
            raise SmokeFail(f"SL не встал: ждали {sl:g}, есть {got_sl:g}")
        if not _close(got_tp, tp, f.tick_size):
            raise SmokeFail(f"TP не встал: ждали {tp:g}, есть {got_tp:g}")
        _ok(f"серверные стопы встали: SL={got_sl:g} TP={got_tp:g}")

        # безубыток: бот ставит SL=вход при цене +3%; здесь цена у входа,
        # а биржа требует SL long строго ниже текущей цены — поднимаем
        # стоп до min(вход, цена-0.1%) и проверяем, что он вырос
        last = await client.get_price(cfg.symbol)
        sl_be = round(min(entry, last * 0.999) / f.tick_size) * f.tick_size
        await client.set_stop_loss_take_profit(cfg.symbol, sl_be, tp)
        got_sl, _ = await client.get_position_stops(cfg.symbol)
        if not _close(got_sl, sl_be, f.tick_size):
            raise SmokeFail(f"BE не перенёс SL: ждали {sl_be:g}, есть {got_sl:g}")
        if got_sl <= sl:
            raise SmokeFail(f"BE не поднял стоп: {got_sl:g} <= {sl:g}")
        _ok(f"безубыток: SL поднят {sl:g} → {got_sl:g} (правило: SL long < цены)")

        await client.close_position(cfg.symbol, pos.size, pos.side)
        if not await _wait_flat(client, cfg.symbol):
            raise SmokeFail("позиция не закрылась")
        opened_by_us = False
        _ok("позиция закрыта")
    finally:
        try:
            await client.cancel_all_orders(cfg.symbol)
            logger.info("finally: все заявки сняты")
            if opened_by_us:
                pos = await client.get_position(cfg.symbol)
                if pos is not None:
                    await client.close_position(cfg.symbol, pos.size, pos.side)
                    logger.warning("finally: позиция закрыта аварийно")
        except Exception as exc:  # noqa: BLE001
            logger.error("finally не прошёл: %s — проверь биржу руками", exc)
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Проверка выставления/снятия лимиток, стопов и BE",
    )
    parser.add_argument(
        "--fill",
        action="store_true",
        help="открыть минимальную позицию, проверить SL/TP/BE и закрыть",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(run(args.fill))
    except SmokeFail as exc:
        logger.error("FAIL %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.error("прервано пользователем")
        sys.exit(130)
    logger.info("SMOKE OK")


if __name__ == "__main__":
    main()
