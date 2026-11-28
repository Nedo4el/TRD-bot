"""Robot Yrovni D — торговля по POC-сигналам скринера.

Запуск (DRY_RUN по умолчанию)::

    .venv\\Scripts\\python.exe -m robot_yrovni_D.main

Проверка на одном сигнале::

    .venv\\Scripts\\python.exe -m robot_yrovni_D.main --once --signal-file signal.json

Сигналы приходят двумя путями: ``POST /api/signal`` на собственный порт
робота (``SIGNAL_PORT``) и поллингом дашборда (``GET /api/poc-signals``,
берутся только ``active``). Скринер менять не нужно.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from aiohttp import ClientSession, ClientTimeout, web

from core.bybit_client import BybitClient
from core.config import Config
from core.logger import get_logger, setup_logging
from robot_yrovni_D.broker import Broker, BybitBroker, OpenOrder
from robot_yrovni_D.config import LevelConfig, load_config
from robot_yrovni_D.json_log import JsonlLogger
from robot_yrovni_D.signals import PocSignal, SignalError, parse_signal
from robot_yrovni_D.state import StateStore
from robot_yrovni_D.strategy import (
    Action,
    LevelStrategy,
    adopted_position,
    position_from_state,
)

logger = get_logger(__name__)

MAX_PROCESSED = 1000
ACTIVE_STATE = "active"


class ProcessedSignals:
    """Идентификаторы уже обработанных сигналов (переживают рестарт)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._ids: list[str] = self._load()

    def __contains__(self, signal_id: str) -> bool:
        return signal_id in self._ids

    def add(self, signal_id: str) -> None:
        if signal_id in self._ids:
            return
        self._ids.append(signal_id)
        self._ids = self._ids[-MAX_PROCESSED:]
        self._save()

    def discard_prefix(self, prefix: str) -> int:
        """Забыть сигналы с id по префиксу; вернуть, сколько забыто."""
        kept = [item for item in self._ids if not item.startswith(prefix)]
        removed = len(self._ids) - len(kept)
        if removed:
            self._ids = kept
            self._save()
        return removed

    def discard(self, signal_id: str) -> bool:
        """Забыть один сигнал: поллер сможет выставить по нему сетку заново."""
        if signal_id not in self._ids:
            return False
        self._ids.remove(signal_id)
        self._save()
        return True

    def _load(self) -> list[str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw][-MAX_PROCESSED:]

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._ids), encoding="utf-8")
            temp.replace(self._path)
        except OSError as exc:
            logger.warning("Список обработанных сигналов не сохранён: %s", exc)


def build_broker(config: LevelConfig) -> tuple[Broker, BybitClient]:
    """Собрать брокер Bybit (REST) с бумажным исполнением в DRY_RUN."""
    core_config = Config(
        api_key=config.api_key,
        api_secret=config.api_secret,
        testnet=config.testnet,
        category=config.category,
        symbol="",
        timeframe=config.atr_timeframe,
        requests_per_second=config.requests_per_second,
    )
    client = BybitClient(core_config)
    broker = BybitBroker(
        client,
        dry_run=config.dry_run,
        atr_period=config.atr_period,
        atr_timeframe=config.atr_timeframe,
    )
    return broker, client


def build_strategy(
    config: LevelConfig,
    broker: Broker,
    log: JsonlLogger,
    state: StateStore | None = None,
) -> LevelStrategy:
    """Собрать стратегию с журналом и брокером."""
    return LevelStrategy(config, broker, log, state=state)


def make_logger(config: LevelConfig) -> JsonlLogger:
    """Журнал событий ``logs/strategy.jsonl``."""
    return JsonlLogger(
        _PROJECT_ROOT / config.log_file,
        rotation_mb=config.log_rotation_mb,
        keep_days=config.log_keep_days,
        dry_run=config.dry_run,
    )


async def handle_signal(
    payload: Mapping[str, Any],
    queue: asyncio.Queue[PocSignal],
    processed: ProcessedSignals,
    log: JsonlLogger,
) -> bool:
    """Разобрать сигнал и поставить в очередь; False — дубль или мусор."""
    try:
        signal = parse_signal(payload)
    except SignalError as exc:
        log.log(
            "ERROR",
            symbol=str(payload.get("symbol") or ""),
            signal_id=str(payload.get("signal_id") or ""),
            error_type="SignalError",
            message=str(exc),
            retry_count=0,
        )
        return False
    if signal.signal_id in processed:
        return False
    processed.add(signal.signal_id)
    await queue.put(signal)
    return True


def for_this_instance(record: Mapping[str, Any], symbol: str) -> bool:
    """Фильтр per-symbol запуска: процесс берёт сигналы только своей монеты."""
    if not symbol:
        return True
    return str(record.get("symbol") or "").strip().upper() == symbol


class SignalServer:
    """HTTP-приёмник сигналов POC-скринера."""

    def __init__(
        self,
        config: LevelConfig,
        queue: asyncio.Queue[PocSignal],
        processed: ProcessedSignals,
        log: JsonlLogger,
    ) -> None:
        self._config = config
        self._queue = queue
        self._processed = processed
        self._log = log

    def app(self) -> web.Application:
        """Собрать aiohttp-приложение с ручками приёма сигналов."""
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_post("/api/signal", self._receive)
        app.router.add_post("/api/poc-signal", self._receive)
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "dry_run": self._config.dry_run},
        )

    async def _receive(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "bad payload"}, status=400)
        accepted = await handle_signal(payload, self._queue, self._processed, self._log)
        return web.json_response(
            {
                "accepted": accepted,
                "signal_id": str(payload.get("signal_id") or ""),
                "duplicate": not accepted,
            },
        )


async def poll_dashboard(
    config: LevelConfig,
    queue: asyncio.Queue[PocSignal],
    processed: ProcessedSignals,
    log: JsonlLogger,
) -> None:
    """Забирать ``active`` сигналы из дашборда (скринер постит туда)."""
    if config.signal_poll_sec <= 0:
        return
    url = config.dashboard_url.rstrip("/") + "/api/poc-signals"
    timeout = ClientTimeout(total=config.poll_timeout_sec)
    while True:
        try:
            async with (
                ClientSession(timeout=timeout) as session,
                session.get(url) as response,
            ):
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                payload = await response.json()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — дашборд может быть недоступен
            log.log(
                "ERROR",
                symbol="",
                signal_id="",
                error_type=type(exc).__name__,
                message=f"dashboard poll: {exc}",
                retry_count=0,
            )
        else:
            for record in payload.get("signals", []):
                if not isinstance(record, dict):
                    continue
                if str(record.get("state") or "") != ACTIVE_STATE:
                    continue
                if not for_this_instance(record, config.symbol):
                    continue
                await handle_signal(record, queue, processed, log)
        await asyncio.sleep(config.signal_poll_sec)


async def consume_signals(
    strategy: LevelStrategy,
    queue: asyncio.Queue[PocSignal],
    log: JsonlLogger,
) -> None:
    """Обрабатывать сигналы из очереди до остановки."""
    while True:
        signal = await queue.get()
        try:
            await strategy.on_poc_signal(signal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Ошибка обработки сигнала %s", signal.signal_id)
            log.log(
                "ERROR",
                symbol=signal.symbol,
                signal_id=signal.signal_id,
                error_type=type(exc).__name__,
                message=str(exc),
                retry_count=0,
            )
        finally:
            queue.task_done()


async def tick_loop(
    strategy: LevelStrategy,
    broker: Broker,
    log: JsonlLogger,
    config: LevelConfig,
) -> None:
    """Тик раз в TICK_SEC: сверить филлы и прогнать проверки стратегии."""
    while True:
        for position in strategy.positions():
            try:
                price = await broker.price(position.symbol)
                await strategy.sync_fills(position, price)
                if position.closed:
                    continue
                actions = await strategy.update_on_tick(price, position)
                if actions != [Action.HOLD]:
                    logger.info(
                        "%s: %s",
                        position.symbol,
                        ", ".join(action.value for action in actions),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Тик %s", position.symbol)
                log.log(
                    "ERROR",
                    symbol=position.symbol,
                    signal_id=position.signal_id,
                    error_type=type(exc).__name__,
                    message=str(exc),
                    retry_count=0,
                )
        strategy.maybe_daily_summary()
        await asyncio.sleep(config.tick_sec)


async def run_once(config: LevelConfig, signal_file: Path) -> int:
    """Прогнать один сигнал из файла (проверка стратегии без цикла)."""
    payload = json.loads(signal_file.read_text(encoding="utf-8-sig"))
    signal = parse_signal(payload)
    log = make_logger(config)
    broker, client = build_broker(config)
    strategy = build_strategy(config, broker, log)
    try:
        orders = await strategy.on_poc_signal(signal)
    finally:
        client.close()
    if not orders:
        print("сигнал отклонён — причина в журнале SIGNAL_SKIPPED")
        return 0
    print(f"выставлено лимиток: {len(orders)}")
    for order in orders:
        print(
            f"  {order.level:>4} {order.side:<4} {order.price:.10g} x {order.qty:.10g}"
        )
    return 0


def _signal_prefix(symbol: str, link_id: str, order_link_prefix: str) -> str | None:
    """Префикс id сигнала из link id ``zk-{symbol}-{poc}-{side}-{level}-{token}``.

    Bybit режет link id до 36 символов, поэтому у длинных символов сторона
    теряется — тогда матчим только по символу и POC.
    """
    parts = link_id[len(order_link_prefix) :].split("-")
    if len(parts) < 2 or not parts[1] or parts[0].upper() != symbol.upper():
        return None
    if len(parts) >= 3 and parts[2] in ("long", "short"):
        return f"{symbol}:{parts[1]}:{parts[2]}:"
    return f"{symbol}:{parts[1]}:"


def _rearm_signals(
    symbol: str,
    own: list[OpenOrder],
    cancelled: list[str],
    config: LevelConfig,
    processed: ProcessedSignals,
) -> int:
    """Разрешить сигналы снятых ордеров: поллер выставит сетку заново."""
    seen: set[str] = set()
    rearmed = 0
    for item in own:
        if item.order_id not in cancelled:
            continue
        prefix = _signal_prefix(symbol, item.link_id, config.order_link_prefix)
        if prefix is None or prefix in seen:
            continue
        seen.add(prefix)
        rearmed += processed.discard_prefix(prefix)
    return rearmed


async def fetch_active_signals(config: LevelConfig) -> list[dict[str, Any]]:
    """Активные POC-сигналы из дашборда; пустой список — дашборд недоступен."""
    url = config.dashboard_url.rstrip("/") + "/api/poc-signals"
    timeout = ClientTimeout(total=config.poll_timeout_sec)
    try:
        async with (
            ClientSession(timeout=timeout) as session,
            session.get(url) as response,
        ):
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            payload = await response.json()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — дашборд может быть недоступен
        logger.warning("реарм: дашборд недоступен: %s", exc)
        return []
    records = payload.get("signals") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    return [item for item in records if isinstance(item, dict)]


def still_approaching(
    record: Mapping[str, Any],
    price: float,
    max_distance_pct: float,
) -> bool:
    """Цена ещё не дошла до уровня и не ушла от него дальше допуска."""
    if str(record.get("state") or "") != ACTIVE_STATE:
        return False
    try:
        entry = float(record.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return False
    direction = str(record.get("direction") or "").lower()
    if entry <= 0 or price <= 0:
        return False
    if abs(price - entry) / entry > max_distance_pct:
        return False
    if direction == "short":
        return price < entry
    if direction == "long":
        return price > entry
    return False


async def rearm_active_signals(
    symbol: str,
    broker: Broker,
    config: LevelConfig,
    processed: ProcessedSignals,
    log: JsonlLogger,
    fetch_active: Callable[[], Awaitable[list[dict[str, Any]]]],
) -> int:
    """Разрешить активные сигналы, до уровня которых цена ещё не дошла.

    Сетка по такому сигналу могла быть снята по TTL, а сам сигнал не
    отработан: цена так и не коснулась уровня. Повторная постановка
    безопасна — позиции и своих ордеров на бирже нет (проверено
    самодиагностикой), а фильтры входа применятся заново.
    """
    if config.kill_switch_file and Path(config.kill_switch_file).exists():
        return 0
    records = await fetch_active()
    if not records:
        return 0
    try:
        price = await broker.price(symbol)
    except Exception as exc:  # noqa: BLE001 — цена не должна ронять старт
        logger.warning("реарм %s: цена недоступна: %s", symbol, exc)
        return 0
    limit = config.max_distance_from_poc_pct
    rearmed = 0
    for record in records:
        if str(record.get("symbol") or "").upper() != symbol:
            continue
        signal_id = str(record.get("signal_id") or "")
        if not signal_id or not still_approaching(record, price, limit):
            continue
        if processed.discard(signal_id):
            rearmed += 1
            logger.info(
                "%s: сигнал %s снова разрешён — цена %.10g не дошла до %.10g",
                symbol,
                signal_id,
                price,
                float(record.get("entry_price") or 0.0),
            )
    return rearmed


async def startup_diagnostic(
    strategy: LevelStrategy,
    broker: Broker,
    log: JsonlLogger,
    config: LevelConfig,
    processed: ProcessedSignals,
    fetch_active: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    state: StateStore | None = None,
) -> None:
    """Сверить старые ордера и позицию по символу с биржей до старта торговли."""
    if not config.startup_check:
        return
    symbol = config.symbol
    if not symbol:
        logger.info("самодиагностика: пропуск — процесс без SYMBOL")
        return
    try:
        orders = await broker.open_orders(symbol)
        position = await broker.position(symbol)
    except Exception as exc:  # noqa: BLE001 — биржа может быть недоступна на старте
        logger.warning("самодиагностика %s: биржа недоступна: %s", symbol, exc)
        log.log("STARTUP_DIAG", symbol=symbol, error=str(exc))
        return

    own = [item for item in orders if item.link_id.startswith(config.order_link_prefix)]
    position_info = None
    if position is not None:
        position_info = {
            "side": position.side,
            "qty": position.qty,
            "avg_price": position.avg_price,
        }

    cancelled: list[str] = []
    blocked = False
    rearmed = 0
    if position is not None:
        blocked = bool(own)
        if own:
            strategy.block_symbol(symbol, "startup_stale_position")
            logger.error(
                "%s: самодиагностика — позиция %s %.10g при %d своих ордерах без "
                "состояния: ордера не трогаю, входы по символу заблокированы",
                symbol,
                position.side,
                position.qty,
                len(own),
            )
        else:
            logger.warning(
                "%s: самодиагностика — позиция %s %.10g без своих ордеров "
                "(возможно, другой робот): не трогаю",
                symbol,
                position.side,
                position.qty,
            )
    elif own and config.startup_cleanup:
        for item in own:
            if await broker.cancel(symbol, item.order_id):
                cancelled.append(item.order_id)
        rearmed = _rearm_signals(symbol, own, cancelled, config, processed)
        logger.warning(
            "%s: самодиагностика — снято старых ордеров %d из %d",
            symbol,
            len(cancelled),
            len(own),
        )
        if rearmed:
            logger.info(
                "%s: самодиагностика — сигнал снова разрешён, сетка встанет заново",
                symbol,
            )
    elif own:
        logger.warning(
            "%s: самодиагностика — висит %d своих ордеров (STARTUP_CLEANUP=false)",
            symbol,
            len(own),
        )
    else:
        logger.info("%s: самодиагностика — чисто", symbol)

    if fetch_active is not None and position is None and config.startup_rearm:
        rearmed += await rearm_active_signals(
            symbol,
            broker,
            config,
            processed,
            log,
            fetch_active,
        )

    log.log(
        "STARTUP_DIAG",
        symbol=symbol,
        own_orders=len(own),
        foreign_orders=len(orders) - len(own),
        position=position_info,
        cancelled=len(cancelled),
        blocked=blocked,
        rearmed=rearmed,
    )


async def run(config: LevelConfig) -> None:
    """Основной цикл: приёмник сигналов + тик-цикл."""
    log = make_logger(config)
    broker, client = build_broker(config)
    strategy = build_strategy(config, broker, log)
    processed = ProcessedSignals(_PROJECT_ROOT / config.processed_file)
    queue: asyncio.Queue[PocSignal] = asyncio.Queue()

    await startup_diagnostic(
        strategy,
        broker,
        log,
        config,
        processed,
        fetch_active=lambda: fetch_active_signals(config),
    )

    runner: web.AppRunner | None = None
    if config.symbol:
        logger.info(
            "robot_yrovni_D: %s, DRY_RUN=%s, поллинг дашборда %s с (без HTTP-приёмника)",
            config.symbol,
            config.dry_run,
            config.signal_poll_sec or "выкл",
        )
    else:
        server = SignalServer(config, queue, processed, log)
        runner = web.AppRunner(server.app())
        await runner.setup()
        site = web.TCPSite(runner, config.signal_host, config.signal_port)
        await site.start()
        logger.info(
            "robot_yrovni_D: приёмник на %s:%s, DRY_RUN=%s, поллинг дашборда %s с",
            config.signal_host,
            config.signal_port,
            config.dry_run,
            config.signal_poll_sec or "выкл",
        )

    tasks = [
        asyncio.create_task(consume_signals(strategy, queue, log)),
        asyncio.create_task(tick_loop(strategy, broker, log, config)),
        asyncio.create_task(poll_dashboard(config, queue, processed, log)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if runner is not None:
            await runner.cleanup()
        client.close()


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    parser = argparse.ArgumentParser(description="Robot Yrovni D — POC-уровни")
    parser.add_argument("--once", action="store_true", help="один сигнал из файла")
    parser.add_argument("--signal-file", type=Path, help="JSON сигнала для --once")
    args = parser.parse_args(argv)

    config = load_config()
    setup_logging(_PROJECT_ROOT / config.bot_log_file, config.log_level)
    if not config.dry_run:
        config.validate_for_live()

    if args.once:
        if args.signal_file is None:
            parser.error("--once требует --signal-file")
        return asyncio.run(run_once(config, args.signal_file))

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("robot_yrovni_D остановлен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
