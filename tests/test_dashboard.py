"""Тесты дашборда: конфиг, хранилище, планировщик, fetcher, settings."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Self

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
from dashboard.settings import AllCoinsSettings
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
        "all",
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


def test_settings_defaults_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AllCoinsSettings: дефолты + save/load JSON round-trip."""
    import dashboard.settings as settings_module

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", tmp_path / "settings.json")

    s = AllCoinsSettings()
    assert s.enabled is True
    assert s.interval_sec == 60
    assert s.atr_period == 14
    assert s.sort_mode == "atr_desc"

    s.min_turnover_24h = 5_000_000
    s.max_symbols = 50
    s.sort_mode = "turnover_desc"
    s.volume_trend = "up"
    s.exclude_symbols = ["BTCUSDT", "SOLUSDT"]
    s.save()

    loaded = AllCoinsSettings.load()
    assert loaded.min_turnover_24h == 5_000_000
    assert loaded.max_symbols == 50
    assert loaded.sort_mode == "turnover_desc"
    assert loaded.volume_trend == "up"
    assert loaded.exclude_symbols == ["BTCUSDT", "SOLUSDT"]
    assert loaded.enabled is True


def test_settings_load_invalid_values_falls_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Некорректные значения в JSON → дефолты."""
    import dashboard.settings as settings_module

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "interval_sec": 999,
                "sort_mode": "bad",
                "volume_trend": "bad",
                "max_symbols": 99999,
            }
        ),
        encoding="utf-8",
    )

    s = AllCoinsSettings.load()
    assert s.interval_sec == 60
    assert s.sort_mode == "atr_desc"
    assert s.volume_trend == "all"
    assert s.max_symbols == 500


def test_scan_all_coins_mocked(
    store: SignalStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """scan_all_coins: фиктивный fetcher → сигналы с ATR и volume trend."""
    import dashboard.adapters as adapters_module
    import dashboard.settings as settings_module
    from dashboard.adapters import scan_all_coins

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", tmp_path / "settings.json")
    s = AllCoinsSettings(enabled=True, min_turnover_24h=1_000_000, max_symbols=10)
    s.save()

    fake_tickers = [
        {
            "symbol": "AAAUSDT",
            "turnover24h": "5000000",
            "priceChangePercent": "5.2",
            "lastPrice": "10.5",
        },
        {
            "symbol": "BBBUSDT",
            "turnover24h": "3000000",
            "priceChangePercent": "-3.1",
            "lastPrice": "0.02",
        },
        {
            "symbol": "BTCUSDT",
            "turnover24h": "99999999",
            "priceChangePercent": "1.0",
            "lastPrice": "100000",
        },
    ]

    class FakeFetcher:
        async def get_tickers(self) -> list[dict]:
            return fake_tickers

        async def get_klines(
            self, symbol: str, interval: str, limit: int = 100
        ) -> list[dict]:
            base = 10.0 if symbol == "AAAUSDT" else 0.02
            return [
                {
                    "open_time": i,
                    "open": base,
                    "high": base * 1.02,
                    "low": base * 0.98,
                    "close": base,
                    "volume": 1000.0 + i * 10,
                    "turnover": base * (1000.0 + i * 10),
                }
                for i in range(limit)
            ]

    monkeypatch.setattr(
        adapters_module, "DashboardFetcher", lambda *a, **kw: FakeFetcher()
    )

    spec = ScreenerSpec(
        name="all", title="Все монеты", dir_name="all", interval_sec=60, max_symbols=10
    )

    async def run() -> list:
        return await scan_all_coins(spec)

    signals = asyncio.run(run())
    symbols = [x.symbol for x in signals]
    assert "BTCUSDT" not in symbols
    assert "AAAUSDT" in symbols
    aaa = next(x for x in signals if x.symbol == "AAAUSDT")
    assert aaa.detail["change_24h_pct"] == 5.2
    assert aaa.detail["atr_pct"] is not None
    assert aaa.detail["volume_trend_pct"] is not None


def test_settings_api_endpoints(
    store: SignalStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """GET/POST /api/settings/all (прямые вызовы хендлеров)."""
    import dashboard.settings as settings_module
    from dashboard.server import api_settings_all_get, api_settings_all_post

    monkeypatch.setattr(settings_module, "SETTINGS_FILE", tmp_path / "settings.json")

    from types import SimpleNamespace

    class FakeScheduler:
        async def trigger(self, name: str | None = None) -> bool:
            return True

    app_state = SimpleNamespace(
        scheduler=FakeScheduler(),
        store=store,
        config=DashboardConfig(enabled=("impulse",)),
    )
    fake_app = SimpleNamespace(state=app_state)

    from dashboard.settings import AllCoinsSettings

    # GET: дефолты
    async def get_test() -> Any:
        request: Any = SimpleNamespace(app=fake_app)
        resp = await api_settings_all_get(request)
        return resp

    resp = asyncio.run(get_test())
    data = resp.body
    assert b"atr_desc" in data

    # POST: сохранить и вернуть
    async def post_test() -> Any:
        async def fake_json() -> dict:
            return {"max_symbols": 50, "sort_mode": "turnover_desc", "enabled": True}

        request: Any = SimpleNamespace(app=fake_app, json=fake_json)
        return await api_settings_all_post(request)

    resp2 = asyncio.run(post_test())
    body2 = json.loads(resp2.body)
    assert body2["ok"] is True
    assert body2["settings"]["max_symbols"] == 50
    assert body2["settings"]["sort_mode"] == "turnover_desc"

    # После save → load должен вернуть то же
    loaded = AllCoinsSettings.load()
    assert loaded.max_symbols == 50
    assert loaded.sort_mode == "turnover_desc"


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
