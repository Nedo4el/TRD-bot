"""Тесты дашборда: конфиг, хранилище, планировщик, fetcher."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Self

import pytest

from dashboard import scheduler as scheduler_module
from dashboard import store as store_module
from dashboard import tiger
from dashboard.config import (
    MIN_INTERVAL_SEC,
    SCREENERS,
    DashboardConfig,
    ScreenerEnv,
    ScreenerSpec,
)
from dashboard.fetcher import DashboardFetcher
from dashboard.models import Signal, make_signal
from dashboard.scheduler import Scheduler
from dashboard.store import SignalStore


def _spec(name: str = "impulse", interval: int = 60) -> ScreenerSpec:
    return ScreenerSpec(
        name=name,
        title="Тест",
        dir_name=f"bot_screener_{name}",
        interval_sec=interval,
        max_symbols=5,
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SignalStore:
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_module, "STATE_FILE", tmp_path / "latest.json")
    return SignalStore([_spec()])


def _signal(symbol: str, score: float, screener: str = "impulse") -> Signal:
    return make_signal(
        screener=screener,
        title="Тест",
        symbol=symbol,
        side="LONG",
        price=1.5,
        score=score,
        label="тест",
        turnover_24h=1_000_000.0,
        found_at="2026-01-01T00:00:00+00:00",
        detail={"move_pct": 5.0},
    )


def test_registry_matches_expected_screeners() -> None:
    names = {spec.name for spec in SCREENERS}
    assert names == {
        "impulse",
        "klin",
        "uzkiy",
        "krugloe",
        "trend",
        "pump",
        "yrovni",
        "zakonomer",
    }
    assert all(spec.interval_sec >= MIN_INTERVAL_SEC for spec in SCREENERS)


def test_screener_env_reads_own_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        "TIMEFRAME=5\nMIN_MOVE_PCT=3.5\nTESTNET=false\n"
        "EXCLUDE_SYMBOLS=BTCUSDT, ETHUSDT\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TIMEFRAME", "60")

    env = ScreenerEnv(tmp_path)

    assert env.string("TIMEFRAME") == "5"
    assert env.number("MIN_MOVE_PCT", 0.0) == 3.5
    assert env.boolean("TESTNET", True) is False
    assert env.names("EXCLUDE_SYMBOLS") == ["BTCUSDT", "ETHUSDT"]
    assert env.integer("MISSING", 7) == 7


def test_dashboard_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASH_PORT", "9000")
    monkeypatch.setenv("DASH_CONCURRENCY", "0")
    monkeypatch.setenv("DASH_MAX_SYMBOLS", "3")
    monkeypatch.setenv("DASH_ENABLED", "impulse, klin")

    config = DashboardConfig.from_env()
    specs = config.resolved_specs()

    assert config.port == 9000
    assert config.concurrency == 1
    assert [spec.name for spec in specs] == ["impulse", "klin"]
    assert all(spec.max_symbols <= 3 for spec in specs)
    assert all(spec.interval_sec >= MIN_INTERVAL_SEC for spec in specs)


def test_store_keeps_best_signal_per_symbol(store: SignalStore) -> None:
    store.set_result(
        "impulse",
        [_signal("AAAUSDT", 10.0), _signal("AAAUSDT", 50.0), _signal("BBBUSDT", 5.0)],
        1.234,
        "2026-01-01T00:01:00+00:00",
    )

    snapshot = store.snapshot()

    assert snapshot["total_signals"] == 2
    assert snapshot["signals"][0]["symbol"] == "AAAUSDT"
    assert snapshot["signals"][0]["score"] == 50.0
    status = snapshot["screeners"][0]
    assert status["count"] == 2
    assert status["running"] is False
    assert status["duration_sec"] == 1.2
    assert status["error"] is None


def test_store_tracks_running_and_errors(store: SignalStore) -> None:
    store.mark_running("impulse")
    assert store.snapshot()["screeners"][0]["running"] is True

    store.set_error("impulse", "RuntimeError: boom", 2.0, "2026-01-01T00:02:00+00:00")

    status = store.snapshot()["screeners"][0]
    assert status["running"] is False
    assert status["error"] == "RuntimeError: boom"
    assert status["last_run_at"] is not None


def test_store_restores_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_module, "STATE_FILE", tmp_path / "latest.json")
    first = SignalStore([_spec()])
    first.set_result(
        "impulse",
        [_signal("AAAUSDT", 42.0)],
        0.5,
        "2026-01-01T00:01:00+00:00",
    )

    second = SignalStore([_spec()])

    snapshot = second.snapshot()
    assert snapshot["total_signals"] == 1
    assert snapshot["signals"][0]["symbol"] == "AAAUSDT"
    assert snapshot["screeners"][0]["count"] == 1
    assert snapshot["screeners"][0]["running"] is False


def test_scheduler_trigger_unknown_screener(store: SignalStore) -> None:
    async def run() -> None:
        scheduler = Scheduler([_spec()], store, DashboardConfig(enabled=("impulse",)))
        assert await scheduler.trigger("unknown") is False
        assert await scheduler.trigger("impulse") is True
        assert await scheduler.trigger() is True
        await scheduler.stop()

    asyncio.run(run())


def test_scheduler_run_writes_result(
    store: SignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_scan(spec: ScreenerSpec) -> list[Signal]:
        return [_signal("AAAUSDT", 42.0, spec.name)]

    monkeypatch.setitem(scheduler_module.ADAPTERS, "impulse", fake_scan)
    scheduler = Scheduler([_spec()], store, DashboardConfig(enabled=("impulse",)))

    asyncio.run(scheduler._run(_spec()))

    snapshot = store.snapshot()
    assert snapshot["total_signals"] == 1
    status = snapshot["screeners"][0]
    assert status["count"] == 1
    assert status["running"] is False
    assert status["error"] is None
    assert status["next_run_at"] is not None


def test_scheduler_run_records_failure(
    store: SignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_scan(spec: ScreenerSpec) -> list[Signal]:
        raise RuntimeError("boom")

    monkeypatch.setitem(scheduler_module.ADAPTERS, "impulse", broken_scan)
    scheduler = Scheduler([_spec()], store, DashboardConfig(enabled=("impulse",)))

    asyncio.run(scheduler._run(_spec()))

    status = store.snapshot()["screeners"][0]
    assert status["running"] is False
    assert "RuntimeError: boom" in status["error"]


def test_scheduler_wait_uses_absolute_clock(
    store: SignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """После задержки следующий запуск не сдвигается (абсолютное расписание)."""
    spec = _spec(interval=5)
    scheduler = Scheduler([spec], store, DashboardConfig(enabled=("impulse",)))

    clock = scheduler._clocks["impulse"]
    start = time.monotonic()
    clock.anchor = start

    # Скан 2с, интервал 5с → до следующего слота ~3с
    time.sleep(2.0)
    pause = clock.tick()
    assert 2.8 < pause < 3.2

    # Скан 8с (> интервала 5с) → пропуск к слоту +5, до слота ~5с
    time.sleep(8.0)
    pause = clock.tick()
    assert 4.8 < pause < 5.2


def test_fetcher_retries_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """При 403/429 fetcher повторяет с backoff, при успехе возвращает ответ."""
    fetcher = DashboardFetcher(
        api_key="k",
        api_secret="s",
        min_delay=0.0,
        max_retries=4,
        retry_delay=0.0,
    )
    calls: list[int] = []

    def fake_http_call(*args: object, **kwargs: object) -> dict:
        calls.append(1)
        if len(calls) < 3:
            exc = Exception("blocked: 403")
            exc.status_code = 403  # type: ignore[attr-defined]
            raise exc
        return {"retCode": 0, "result": {"list": []}}

    monkeypatch.setattr(
        fetcher,
        "_http",
        type("FakeHTTP", (), {"get_tickers": fake_http_call})(),
    )

    async def run() -> list[dict]:
        return await fetcher.get_tickers()

    result = asyncio.run(run())
    assert result == []
    assert len(calls) == 3


def test_fetcher_returns_empty_after_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если все попытки 403 — get_tickers возвращает пустой список."""
    fetcher = DashboardFetcher(
        api_key="k",
        api_secret="s",
        min_delay=0.0,
        max_retries=3,
        retry_delay=0.0,
    )

    def always_blocked(*args: object, **kwargs: object) -> dict:
        exc = Exception("blocked: 429")
        exc.status_code = 429  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(
        fetcher,
        "_http",
        type("FakeHTTP", (), {"get_tickers": always_blocked})(),
    )

    async def run() -> list[dict]:
        return await fetcher.get_tickers()

    assert asyncio.run(run()) == []
    payload = tiger.build_request("btcusdt", request_id="1")

    assert payload == {
        "type": "SETLINKSYMBOL",
        "requestId": "1",
        "e": "BYBIT",
        "m": "FUTURES",
        "symbol": "BTCUSDT",
        "linkGroup": "A",
    }


def test_tiger_request_multiex() -> None:
    payload = tiger.build_request("ETHUSDT", mode="multiex", request_id="2")

    assert payload["type"] == "OPENMULTIEXSYMBOL"
    assert "linkGroup" not in payload
    assert payload["symbol"] == "ETHUSDT"


def test_tiger_parse_response_success_and_error() -> None:
    ok = json.dumps(
        {
            "title": "Successful request",
            "message": "Symbol opened successfully.",
            "response": {"type": "successfully"},
        }
    )
    error = json.dumps(
        {
            "title": "Symbol not found",
            "message": "Trading symbol not found.",
            "response": {
                "type": "error",
                "error": {"message": "Symbol not found", "code": 400},
            },
        }
    )

    assert tiger.parse_response(ok) == (True, "Symbol opened successfully.")
    assert tiger.parse_response(error) == (False, "Symbol not found")
    assert tiger.parse_response("pong")[0] is False


def test_tiger_send_without_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenSocket:
        async def __aenter__(self) -> Self:
            raise OSError("refused")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        tiger.websockets,
        "connect",
        lambda *args, **kwargs: BrokenSocket(),
    )

    async def run() -> None:
        ok, message = await tiger.send(tiger.build_request("BTCUSDT"))
        assert ok is False
        assert "Tiger" in message

    asyncio.run(run())


def test_tiger_spot_symbol_candidates() -> None:
    assert tiger.spot_symbols("1000PEPEUSDT") == ["1000PEPEUSDT", "PEPEUSDT"]
    assert tiger.spot_symbols("1000000MOGUSDT") == [
        "1000000MOGUSDT",
        "MOGUSDT",
    ]
    assert tiger.spot_symbols("btcusdt") == ["BTCUSDT"]


def test_tiger_open_spot_book_falls_back_to_stripped_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send(
        payload: dict[str, object],
        *,
        url: str,
        timeout: float,
    ) -> tuple[bool, str]:
        calls.append(payload)
        if payload["symbol"] == "PEPEUSDT":
            return True, "ok"
        return False, "Symbol not found"

    monkeypatch.setattr(tiger, "send", fake_send)

    async def run() -> tuple[bool, str]:
        return await tiger.open_spot_book("1000PEPEUSDT")

    ok, message = asyncio.run(run())

    assert ok is True
    assert "PEPEUSDT" in message
    assert [call["m"] for call in calls] == ["SPOT", "SPOT", "SPOT"]


def test_tiger_open_combo_links_then_opens_spot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_send(
        payload: dict[str, object],
        *,
        url: str,
        timeout: float,
    ) -> tuple[bool, str]:
        calls.append(payload)
        return True, "Symbol linked successfully."

    monkeypatch.setattr(tiger, "send", fake_send)

    async def run() -> tuple[bool, str]:
        return await tiger.open_combo("BTCUSDT", link_group="Y")

    ok, message = asyncio.run(run())

    assert ok is True
    assert "спот BTCUSDT" in message
    assert calls[0]["type"] == "SETLINKSYMBOL"
    assert calls[0]["linkGroup"] == "Y"
    assert calls[1]["type"] == "OPENMULTIEXSYMBOL"
    assert calls[1]["m"] == "SPOT"
