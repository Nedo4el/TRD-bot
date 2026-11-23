"""Тесты robot_yrovni_D: вход тремя лимитками, BE, трейлинг, журнал, парсер."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from robot_yrovni_D.broker import OrderResult, PositionInfo
from robot_yrovni_D.config import LevelConfig
from robot_yrovni_D.json_log import JsonlLogger
from robot_yrovni_D.main import MAX_PROCESSED, ProcessedSignals, handle_signal
from robot_yrovni_D.signals import PocSignal, SignalError, parse_signal
from robot_yrovni_D.strategy import Action, LevelStrategy, PositionState

SYMBOL = "BTCUSDT"


class Clock:
    """Управляемые часы: тесты не зависят от реального времени."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBroker:
    """Брокер-заглушка: помнит ордера и «исполняет» их по команде теста."""

    dry_run = True

    def __init__(
        self,
        *,
        price: float = 50.0,
        atr_pct: float = 0.005,
        min_notional_value: float = 1.0,
        funding_ts: float | None = None,
    ) -> None:
        self._price = price
        self._atr_pct = atr_pct
        self._min_notional = min_notional_value
        self._funding_ts = funding_ts
        self.limits: dict[str, dict[str, Any]] = {}
        self.stops: dict[str, dict[str, Any]] = {}
        self.takes: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self.closed_qty = 0.0
        self.qty = 0.0
        self.avg_price = 0.0
        self._seq = 0

    def _order_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    async def price(self, symbol: str) -> float:
        return self._price

    async def atr_pct(self, symbol: str) -> float | None:
        return self._atr_pct

    async def next_funding_ts(self, symbol: str) -> float | None:
        return self._funding_ts

    async def min_notional(self, symbol: str) -> float:
        return self._min_notional

    async def place_limit(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        order_id = self._order_id("L")
        self.limits[order_id] = {
            "side": side,
            "price": price,
            "qty": qty,
            "link_id": link_id,
        }
        return OrderResult(order_id, link_id, side, qty, "limit", price=price)

    async def place_stop(
        self,
        symbol: str,
        side: str,
        trigger: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        order_id = self._order_id("S")
        self.stops[order_id] = {
            "side": side,
            "trigger": trigger,
            "qty": qty,
            "link_id": link_id,
        }
        return OrderResult(order_id, link_id, side, qty, "stop", trigger=trigger)

    async def place_take(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        link_id: str,
    ) -> OrderResult:
        order_id = self._order_id("T")
        self.takes[order_id] = {
            "side": side,
            "price": price,
            "qty": qty,
            "link_id": link_id,
        }
        return OrderResult(order_id, link_id, side, qty, "take", price=price)

    async def cancel(self, symbol: str, order_id: str) -> bool:
        self.cancelled.append(order_id)
        self.limits.pop(order_id, None)
        self.stops.pop(order_id, None)
        self.takes.pop(order_id, None)
        return True

    async def cancel_all(self, symbol: str) -> None:
        self.limits.clear()
        self.stops.clear()
        self.takes.clear()

    async def market_close(self, symbol: str, qty: float, position_side: str) -> None:
        self.closed_qty = qty
        self.qty = 0.0

    async def open_orders(self, symbol: str) -> list[Any]:
        return []

    async def position(self, symbol: str) -> PositionInfo | None:
        if self.qty <= 0:
            return None
        return PositionInfo(qty=self.qty, side="Buy", avg_price=self.avg_price)

    def fill(self, price: float, qty: float) -> None:
        """Смоделировать исполнение лимитки."""
        total = self.qty + qty
        self.avg_price = (self.avg_price * self.qty + price * qty) / total
        self.qty = total


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Прогнать корутину без pytest-asyncio."""
    return asyncio.run(coro)


def make_config(**overrides: Any) -> LevelConfig:
    """Конфиг стратегии с изолированными путями и выключенным kill-switch."""
    base: dict[str, Any] = {
        "api_key": "key",
        "api_secret": "secret",
        "testnet": True,
        "dry_run": True,
        "deposit_usd": 100.0,
        "position_pct": 6.0,
        "kill_switch_file": "",
        "signal_poll_sec": 0.0,
    }
    base.update(overrides)
    return LevelConfig(**base)


def make_strategy(
    broker: FakeBroker,
    tmp_path: Path,
    clock: Clock,
    **overrides: Any,
) -> LevelStrategy:
    """Стратегия с журналом в tmp."""
    log = JsonlLogger(tmp_path / "strategy.jsonl", dry_run=True, clock=clock)
    return LevelStrategy(
        make_config(**overrides),
        broker,
        log,
        clock=clock,
    )


def make_signal(
    *,
    side: str = "long",
    poc: float = 50.0,
    price: float = 50.0,
    confidence: float = 1.0,
    symbol: str = SYMBOL,
    signal_id: str | None = None,
) -> PocSignal:
    """Нормализованный сигнал POC."""
    return PocSignal(
        signal_id=signal_id or f"{symbol}:{poc}:{side}:1",
        symbol=symbol,
        poc_price=poc,
        side_hint=side,
        confidence=confidence,
        current_price=price,
    )


def events(tmp_path: Path) -> list[dict[str, Any]]:
    """Прочитать JSONL-журнал теста."""
    path = tmp_path / "strategy.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def actions_of(
    strategy: LevelStrategy,
    position: PositionState,
    price: float,
) -> list[Action]:
    """Один тик по позиции."""
    return run(strategy.update_on_tick(price, position))


def open_position(
    strategy: LevelStrategy,
    broker: FakeBroker,
    signal: PocSignal,
    fill_price: float,
) -> PositionState:
    """Выставить лимитки и смоделировать один филл."""
    orders = run(strategy.on_poc_signal(signal))
    assert orders, "сигнал должен быть принят"
    broker.fill(fill_price, orders[0].qty)
    position = strategy.position(signal.symbol)
    assert position is not None
    run(strategy.sync_fills(position, fill_price))
    return position


# ------------------------------- тесты 1-2: три лимитки ----------------------


def test_long_signal_places_three_buy_limits(tmp_path: Path) -> None:
    """POC=50, long → три BUY-лимитки 51/50/49."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())

    orders = run(strategy.on_poc_signal(make_signal()))

    assert [order.level for order in orders] == ["up", "mid", "down"]
    assert [round(order.price, 8) for order in orders] == [51.0, 50.0, 49.0]
    assert {order.side for order in orders} == {"Buy"}
    assert next(iter(broker.stops.values()))["trigger"] == pytest.approx(45.0)
    assert sum(order.qty for order in orders) == pytest.approx(0.12)


def test_short_signal_places_three_sell_limits(tmp_path: Path) -> None:
    """POC=50, short → три SELL-лимитки на тех же ценах."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())

    orders = run(strategy.on_poc_signal(make_signal(side="short")))

    assert [round(order.price, 8) for order in orders] == [51.0, 50.0, 49.0]
    assert {order.side for order in orders} == {"Sell"}
    assert next(iter(broker.stops.values()))["trigger"] == pytest.approx(55.0)


# --------------------------- тесты 3-5: BE и лимитки ------------------------


def test_long_be_cancels_unfilled_limits(tmp_path: Path) -> None:
    """LONG: одна лимитка исполнена, +1% → стоп в BE и отмена двух лимиток."""
    broker = FakeBroker()
    clock = Clock()
    strategy = make_strategy(broker, tmp_path, clock)
    position = open_position(
        strategy,
        broker,
        make_signal(),
        fill_price=50.0,
    )

    actions = actions_of(strategy, position, 50.5)

    assert actions == [Action.MOVE_BE, Action.CANCEL_PENDING_LIMITS]
    assert position.be_done is True
    assert position.stop is not None
    assert position.stop.trigger == pytest.approx(50.0)
    assert position.open_limits() == []
    assert any(e["action"] == "STOP_MOVED_BE" for e in events(tmp_path))


def test_short_be_cancels_unfilled_limits(tmp_path: Path) -> None:
    """SHORT: одна лимитка исполнена, -1% → стоп в BE и отмена двух лимиток."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    position = open_position(
        strategy,
        broker,
        make_signal(side="short"),
        fill_price=50.0,
    )

    actions = actions_of(strategy, position, 49.5)

    assert actions == [Action.MOVE_BE, Action.CANCEL_PENDING_LIMITS]
    assert position.stop is not None
    assert position.stop.trigger == pytest.approx(50.0)
    assert position.open_limits() == []


def test_long_without_profit_keeps_limits(tmp_path: Path) -> None:
    """LONG: цена ушла вниз без +1% → BE нет, лимитки остаются."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    position = open_position(
        strategy,
        broker,
        make_signal(),
        fill_price=49.0,
    )

    actions = actions_of(strategy, position, 49.3)

    assert actions == [Action.HOLD]
    assert position.be_done is False
    assert len(position.open_limits()) == 2
    assert position.stop is not None
    assert position.stop.trigger < 49.0


# ------------------------ тесты 6-7: тейк и трейлинг -------------------------


def test_long_take_activated_then_trail_closes(tmp_path: Path) -> None:
    """LONG: +7% включает тейк и трейлинг, откат 3% закрывает по рынку."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    position = open_position(
        strategy,
        broker,
        make_signal(),
        fill_price=50.0,
    )
    broker.fill(50.0, 0.12 - broker.qty)  # добираем остаток объёма
    run(strategy.sync_fills(position, 50.0))

    assert actions_of(strategy, position, 50.5) == [Action.MOVE_BE]
    assert Action.ACTIVATE_TAKE in actions_of(strategy, position, 53.5)
    assert position.take is not None
    assert position.take.price == pytest.approx(53.5)

    actions = actions_of(strategy, position, 51.8)

    assert Action.CLOSE_TRAIL in actions
    closed = [e for e in events(tmp_path) if e["action"] == "POSITION_CLOSED"]
    assert closed and closed[-1]["reason"] == "trail"
    assert broker.closed_qty > 0


def test_short_take_activated_then_trail_closes(tmp_path: Path) -> None:
    """SHORT: -7% включает тейк и трейлинг, откат 3% закрывает по рынку."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    position = open_position(
        strategy,
        broker,
        make_signal(side="short"),
        fill_price=50.0,
    )
    broker.fill(50.0, 0.12 - broker.qty)
    run(strategy.sync_fills(position, 50.0))

    assert actions_of(strategy, position, 49.5) == [Action.MOVE_BE]
    assert Action.ACTIVATE_TAKE in actions_of(strategy, position, 46.5)
    assert position.take is not None
    assert position.take.price == pytest.approx(46.5)

    actions = actions_of(strategy, position, 48.13)

    assert Action.CLOSE_TRAIL in actions
    closed = [e for e in events(tmp_path) if e["action"] == "POSITION_CLOSED"]
    assert closed and closed[-1]["reason"] == "trail"


# ------------------------ тест 8: две лимитки исполнены ----------------------


def test_two_fills_average_entry_drives_stop(tmp_path: Path) -> None:
    """Две из трёх лимиток: entry — средняя, стоп и BE считаются от неё."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    orders = run(strategy.on_poc_signal(make_signal()))
    position = strategy.position(SYMBOL)
    assert position is not None

    broker.fill(49.0, orders[2].qty)
    run(strategy.sync_fills(position, 49.0))
    broker.fill(50.0, orders[1].qty)
    run(strategy.sync_fills(position, 50.0))

    assert position.qty == pytest.approx(broker.qty)
    assert position.avg_entry == pytest.approx(broker.avg_price)
    assert position.avg_entry == pytest.approx(
        (49.0 * orders[2].qty + 50.0 * orders[1].qty) / (orders[2].qty + orders[1].qty),
    )
    assert position.stop is not None
    assert position.stop.trigger == pytest.approx(position.avg_entry * 0.9)

    actions_of(strategy, position, position.avg_entry * 1.01)
    assert position.stop is not None
    assert position.stop.trigger == pytest.approx(position.avg_entry)


# --------------------------- тесты 9-10: отмены ------------------------------


def test_price_away_from_poc_cancels_all(tmp_path: Path) -> None:
    """Цена ушла +5% от POC без исполнения → CANCEL_ALL."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    run(strategy.on_poc_signal(make_signal()))
    position = strategy.position(SYMBOL)
    assert position is not None

    actions = actions_of(strategy, position, 52.6)

    assert actions == [Action.CANCEL_ALL]
    assert position.open_limits() == []
    cancelled = [e for e in events(tmp_path) if e["action"] == "LIMITS_CANCELLED"]
    assert cancelled and cancelled[-1]["reason"] == "distance"


def test_max_hold_closes_position(tmp_path: Path) -> None:
    """MAX_HOLD_SEC истёк → POSITION_CLOSED reason=max_hold."""
    broker = FakeBroker()
    clock = Clock()
    strategy = make_strategy(broker, tmp_path, clock)
    position = open_position(strategy, broker, make_signal(), fill_price=50.0)
    clock.advance(86400.0)

    actions = actions_of(strategy, position, 50.0)

    assert Action.CLOSE_MAX_HOLD in actions
    closed = [e for e in events(tmp_path) if e["action"] == "POSITION_CLOSED"]
    assert closed and closed[-1]["reason"] == "max_hold"


# --------------------------- тесты 11-12: фильтры ----------------------------


def test_atr_above_limit_skips_signal(tmp_path: Path) -> None:
    """ATR > 2% → SIGNAL_SKIPPED reason=atr_too_high."""
    broker = FakeBroker(atr_pct=0.03)
    strategy = make_strategy(broker, tmp_path, Clock())

    orders = run(strategy.on_poc_signal(make_signal()))

    assert orders == []
    skipped = [e for e in events(tmp_path) if e["action"] == "SIGNAL_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "atr_too_high"


def test_low_confidence_skips_signal(tmp_path: Path) -> None:
    """confidence < 0.6 → SIGNAL_SKIPPED reason=low_confidence."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())

    orders = run(strategy.on_poc_signal(make_signal(confidence=0.5)))

    assert orders == []
    skipped = [e for e in events(tmp_path) if e["action"] == "SIGNAL_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "low_confidence"
    assert broker.limits == {}


# --------------------------- дополнительные проверки ------------------------


def test_parser_accepts_screener_and_poc_contracts() -> None:
    """Парсер понимает и POC_REACHED, и текущий сигнал скринера."""
    reached = parse_signal(
        {
            "type": "POC_REACHED",
            "symbol": "solusdt",
            "poc_price": "45.29",
            "side_hint": "long",
            "confidence": 0.84,
            "vah": 47.1,
            "val": 43.2,
            "current_price": 45.8,
            "signal_id": "s1",
        },
    )
    assert reached.symbol == "SOLUSDT"
    assert reached.poc_price == pytest.approx(45.29)
    assert reached.confidence == pytest.approx(0.84)
    assert reached.vah == pytest.approx(47.1)

    cluster = parse_signal(
        {
            "type": "POC_CLUSTER",
            "symbol": "AKEUSDT",
            "direction": "short",
            "entry_price": 0.005219,
            "price": 0.005259,
            "signal_id": "AKEUSDT:0.005219:short:3",
        },
    )
    assert cluster.side_hint == "short"
    assert cluster.poc_price == pytest.approx(0.005219)
    assert cluster.confidence == 1.0
    assert cluster.current_price == pytest.approx(0.005259)


def test_parser_rejects_foreign_signal() -> None:
    """Чужая схема и отсутствие цены — ошибка сигнала."""
    with pytest.raises(SignalError):
        parse_signal({"type": "IMPULSE", "symbol": "BTCUSDT", "poc_price": 1.0})
    with pytest.raises(SignalError):
        parse_signal({"symbol": "BTCUSDT", "side_hint": "long"})


def test_jsonl_logger_marks_dry_run_and_survives_bad_path(tmp_path: Path) -> None:
    """Журнал пишет dry_run и не падает на недоступном пути."""
    clock = Clock()
    log = JsonlLogger(tmp_path / "sub" / "strategy.jsonl", dry_run=True, clock=clock)
    record = log.log("LIMITS_PLACED", symbol=SYMBOL, orders=[])
    assert record["dry_run"] is True

    lines = (tmp_path / "sub" / "strategy.jsonl").read_text(encoding="utf-8")
    parsed = json.loads(lines.strip())
    assert parsed["action"] == "LIMITS_PLACED"
    assert parsed["dry_run"] is True

    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    broken = JsonlLogger(blocker / "strategy.jsonl", dry_run=True, clock=clock)
    broken.log("ERROR", symbol=SYMBOL)  # не должно бросить исключение


def test_link_id_is_sanitized_and_short(tmp_path: Path) -> None:
    """orderLinkId: без ':', не длиннее 36 символов."""
    broker = FakeBroker()
    strategy = make_strategy(broker, tmp_path, Clock())
    signal = make_signal(
        signal_id="AKEUSDT:0.005219:long:12345678901234567890",
    )

    run(strategy.on_poc_signal(signal))

    link_ids = [item["link_id"] for item in broker.limits.values()]
    assert link_ids
    assert all(len(link_id) <= 36 for link_id in link_ids)
    assert all(":" not in link_id for link_id in link_ids)
    assert len(set(link_ids)) == len(link_ids)
    assert all(link_id.startswith("zk-") for link_id in link_ids)


def test_small_notional_skips_signal(tmp_path: Path) -> None:
    """Notional одной лимитки ниже биржевого минимума → min_notional."""
    broker = FakeBroker(min_notional_value=5.0)
    strategy = make_strategy(broker, tmp_path, Clock(), deposit_usd=10.0)

    orders = run(strategy.on_poc_signal(make_signal()))

    assert orders == []
    skipped = [e for e in events(tmp_path) if e["action"] == "SIGNAL_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "min_notional"


def test_cooldown_blocks_new_signal_after_exit(tmp_path: Path) -> None:
    """После выхода COOLDOWN_SEC блокирует новый сигнал по тому же символу."""
    broker = FakeBroker()
    clock = Clock()
    strategy = make_strategy(broker, tmp_path, clock)
    position = open_position(strategy, broker, make_signal(), fill_price=50.0)
    clock.advance(86400.0)
    actions_of(strategy, position, 50.0)
    assert position.closed is True

    fresh = make_signal(signal_id="BTCUSDT:50:long:2")
    orders = run(strategy.on_poc_signal(fresh))

    assert orders == []
    skipped = [e for e in events(tmp_path) if e["action"] == "SIGNAL_SKIPPED"]
    assert skipped and skipped[-1]["reason"] == "cooldown"


def test_tick_snapshot_written_not_every_tick(tmp_path: Path) -> None:
    """TICK_SNAPSHOT пишется не чаще TICK_SNAPSHOT_SEC."""
    broker = FakeBroker()
    clock = Clock()
    strategy = make_strategy(broker, tmp_path, clock)
    position = open_position(strategy, broker, make_signal(), fill_price=50.0)

    actions_of(strategy, position, 50.05)
    actions_of(strategy, position, 50.05)
    clock.advance(61.0)
    actions_of(strategy, position, 50.05)

    snapshots = [e for e in events(tmp_path) if e["action"] == "TICK_SNAPSHOT"]
    assert len(snapshots) == 1


def test_jsonl_logger_rotates_by_size(tmp_path: Path) -> None:
    """Журнал переписывается в отдельный файл при достижении лимита размера."""
    clock = Clock()
    path = tmp_path / "strategy.jsonl"
    log = JsonlLogger(path, rotation_mb=0.001, keep_days=0, clock=clock)

    for index in range(60):
        log.log("TICK_SNAPSHOT", symbol=SYMBOL, index=index)

    rotated = list(tmp_path.glob("strategy-*.jsonl"))
    assert rotated, "старый файл должен быть ротирован"
    assert path.exists(), "текущий файл должен быть создан заново"
    assert rotated[0].stat().st_size >= 1024


def test_jsonl_logger_deletes_expired_rotated_files(tmp_path: Path) -> None:
    """Ротированные файлы старше LOG_KEEP_DAYS удаляются, свежие остаются."""
    clock = Clock()
    path = tmp_path / "strategy.jsonl"
    stale = tmp_path / "strategy-20231101-000000.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    old_moment = clock.now - 2 * 86400.0
    os.utime(stale, (old_moment, old_moment))
    fresh = tmp_path / "strategy-20231122-000000.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")

    log = JsonlLogger(path, keep_days=1, clock=clock)
    log.log("ERROR", symbol=SYMBOL)

    assert not stale.exists()
    assert fresh.exists()


def test_processed_signals_dedupe_and_persist(tmp_path: Path) -> None:
    """Обработанные сигналы не дублируются и переживают рестарт."""
    path = tmp_path / "processed.json"
    processed = ProcessedSignals(path)

    assert "s1" not in processed
    processed.add("s1")
    processed.add("s1")

    assert "s1" in processed
    assert json.loads(path.read_text(encoding="utf-8")) == ["s1"]

    reloaded = ProcessedSignals(path)
    assert "s1" in reloaded
    reloaded.add("s1")
    assert json.loads(path.read_text(encoding="utf-8")) == ["s1"]


def test_processed_signals_keeps_last_thousand(tmp_path: Path) -> None:
    """Список не растёт бесконечно: хранится последняя тысяча id."""
    path = tmp_path / "processed.json"
    processed = ProcessedSignals(path)

    for index in range(MAX_PROCESSED + 5):
        processed.add(f"sig-{index}")

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored) == MAX_PROCESSED
    assert "sig-0" not in stored
    assert f"sig-{MAX_PROCESSED + 4}" in stored


def test_handle_signal_ignores_duplicate(tmp_path: Path) -> None:
    """Один и тот же signal_id ставится в очередь только один раз."""
    clock = Clock()
    log = JsonlLogger(tmp_path / "strategy.jsonl", dry_run=True, clock=clock)
    processed = ProcessedSignals(tmp_path / "processed.json")
    payload = {
        "type": "POC_REACHED",
        "symbol": SYMBOL,
        "poc_price": 50.0,
        "side_hint": "long",
        "confidence": 0.9,
        "current_price": 50.0,
        "signal_id": "dup-1",
    }

    async def scenario() -> tuple[bool, bool, asyncio.Queue[PocSignal]]:
        queue: asyncio.Queue[PocSignal] = asyncio.Queue()
        first = await handle_signal(payload, queue, processed, log)
        second = await handle_signal(dict(payload), queue, processed, log)
        return first, second, queue

    first, second, queue = run(scenario())

    assert first is True
    assert second is False
    assert queue.qsize() == 1


def test_daily_summary_counts_closed_positions(tmp_path: Path) -> None:
    """DAILY_SUMMARY появляется при смене суток и считает сделки."""
    broker = FakeBroker()
    clock = Clock()
    strategy = make_strategy(broker, tmp_path, clock)
    position = open_position(strategy, broker, make_signal(), fill_price=50.0)
    actions_of(strategy, position, 50.6)
    broker.qty = 0.0  # биржа закрыла позицию по BE
    run(strategy.sync_fills(position, 50.6))

    clock.advance(86400.0 * 2)
    summary = strategy.maybe_daily_summary()

    assert summary is not None
    assert summary["positions_closed"] == 1
    summary_events = [e for e in events(tmp_path) if e["action"] == "DAILY_SUMMARY"]
    assert summary_events and summary_events[-1]["positions_closed"] == 1
