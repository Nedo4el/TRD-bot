"""Тесты дашборда: конфиг, хранилище, планировщик, fetcher, settings."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from bot_screener_yrovni.poc_scanner.signals import POCSignal
from dashboard import progress, tiger
from dashboard import scheduler as scheduler_module
from dashboard import store as store_module
from dashboard.adapters import scan_poc
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
        found_at=store_module.now_iso(),
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
        "zakonomer",
        "yrovni",
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


def test_store_keeps_signals_when_scan_returns_nothing(store: SignalStore) -> None:
    store.set_result(
        "impulse",
        [_signal("AAAUSDT", 10.0)],
        1.0,
        "2026-01-01T00:01:00+00:00",
    )

    store.set_result("impulse", [], 0.5, "2026-01-01T00:02:00+00:00")

    snapshot = store.snapshot()
    assert snapshot["total_signals"] == 1
    assert snapshot["signals"][0]["symbol"] == "AAAUSDT"
    assert snapshot["screeners"][0]["count"] == 1


def test_store_live_screener_clears_signals_when_scan_returns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_module, "STATE_FILE", tmp_path / "latest.json")
    spec = ScreenerSpec(
        name="yrovni",
        title="Уровни POC",
        dir_name="bot_screener_yrovni",
        interval_sec=300,
        max_symbols=25,
        live_signals=True,
    )
    store = SignalStore([spec])
    store.set_result(
        "yrovni",
        [_signal("AKEUSDT", 0.0, screener="yrovni")],
        1.0,
        "2026-01-01T00:01:00+00:00",
    )

    store.set_result("yrovni", [], 0.5, "2026-01-01T00:02:00+00:00")

    snapshot = store.snapshot()
    assert snapshot["total_signals"] == 0
    assert snapshot["screeners"][0]["count"] == 0


def test_store_clear_signals_removes_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_module, "STATE_FILE", tmp_path / "latest.json")
    store = SignalStore([_spec("impulse"), _spec("klin")])
    store.set_result("impulse", [_signal("AAAUSDT", 1.0)], 1.0, "")
    store.set_result("klin", [_signal("BBBUSDT", 2.0, screener="klin")], 1.0, "")

    cleared = store.clear_signals()

    assert cleared == 2
    snapshot = store.snapshot()
    assert snapshot["total_signals"] == 0
    assert all(item["count"] == 0 for item in snapshot["screeners"])
    reloaded = SignalStore([_spec("impulse"), _spec("klin")])
    assert reloaded.snapshot()["total_signals"] == 0


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


def test_store_restore_drops_stale_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После рестарта старые сигналы (старше TTL) не показываем."""
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_module, "STATE_FILE", tmp_path / "latest.json")
    first = SignalStore([_spec()])
    stale = make_signal(
        screener="impulse",
        title="Тест",
        symbol="OLDUSDT",
        score=42.0,
        found_at="2020-01-01T00:00:00+00:00",
    )
    first.set_result("impulse", [stale], 0.5, "2020-01-01T00:01:00+00:00")

    second = SignalStore([_spec()])

    snapshot = second.snapshot()
    assert snapshot["total_signals"] == 0
    assert snapshot["screeners"][0]["count"] == 0


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
    assert status["next_run_at"] == ""


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
    assert status["progress"] is None


def test_progress_reports_percent_and_eta() -> None:
    progress.start("impulse")
    progress.set_total("impulse", 20)
    progress.advance("impulse", 5)

    snap = progress.snapshot("impulse")

    assert snap is not None
    assert snap["total"] == 20
    assert snap["done"] == 5
    assert snap["percent"] == 25.0
    assert snap["eta_sec"] is not None
    assert snap["eta_sec"] >= 0.0

    progress.clear("impulse")
    assert progress.snapshot("impulse") is None


def test_store_snapshot_includes_scan_progress(store: SignalStore) -> None:
    progress.start("impulse")
    progress.set_total("impulse", 10)
    progress.advance("impulse", 4)

    status = store.snapshot()["screeners"][0]

    assert status["progress"]["done"] == 4
    assert status["progress"]["total"] == 10

    progress.clear("impulse")
    assert store.snapshot()["screeners"][0]["progress"] is None


def test_scheduler_clears_progress_after_run(
    store: SignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any] | None] = []

    async def fake_scan(spec: ScreenerSpec) -> list[Signal]:
        progress.set_total(spec.name, 2)
        progress.advance(spec.name, 2)
        seen.append(progress.snapshot(spec.name))
        return []

    monkeypatch.setitem(scheduler_module.ADAPTERS, "impulse", fake_scan)
    scheduler = Scheduler([_spec()], store, DashboardConfig(enabled=("impulse",)))

    asyncio.run(scheduler._run(_spec()))

    assert seen and seen[0] is not None
    assert seen[0]["done"] == 2
    assert progress.snapshot("impulse") is None


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


def test_scan_trend_uses_htf_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Вкладка «Тренд» считает по HTF (1h); без HTF сигнала нет."""
    import dashboard.adapters as adapters_module
    from dashboard.adapters import scan_trend

    class FakeEnv:
        def number(self, key: str, default: float = 0.0) -> float:
            return {"MIN_TURNOVER_24H": 1_000_000.0}.get(key, default)

        def integer(self, key: str, default: int = 0) -> int:
            return {"LOOKBACK_BARS": 300, "HTF_LOOKBACK_BARS": 300}.get(key, default)

        def string(self, key: str, default: str = "") -> str:
            return {"TIMEFRAME": "5", "HTF_TIMEFRAME": "60"}.get(key, default)

        def names(self, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
            return default

    def candles(count: int, base: float, step: float) -> list[dict]:
        rows = []
        for i in range(count):
            close = base + i * step
            rows.append(
                {
                    "open_time": i,
                    "open": close,
                    "high": close + 0.05,
                    "low": close - 0.05,
                    "close": close,
                    "volume": 1000.0,
                }
            )
        return rows

    class FakeFetcher:
        async def get_filtered_symbols(
            self, min_turnover: float
        ) -> list[tuple[str, float]]:
            return [("AAAUSDT", 50_000_000.0), ("BBBUSDT", 60_000_000.0)]

        async def get_klines(
            self, symbol: str, interval: str, limit: int = 100
        ) -> list[dict]:
            if interval == "60":
                return candles(limit, 100.0, 0.5) if symbol == "AAAUSDT" else []
            step = 0.0 if symbol == "AAAUSDT" else 0.5
            return candles(limit, 100.0, step)

    monkeypatch.setattr(adapters_module, "_env", lambda spec: FakeEnv())
    monkeypatch.setattr(adapters_module, "_new_fetcher", lambda env: FakeFetcher())

    signals = asyncio.run(scan_trend(_spec("trend")))

    assert [s.symbol for s in signals] == ["AAAUSDT"]
    sig = signals[0]
    assert sig.side == "LONG"
    assert sig.detail["timeframe"] == "5"
    assert sig.detail["htf_timeframe"] == "60"
    assert sig.detail["htf_adx"] > 0


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
        min_interval=0.0,
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
        min_interval=0.0,
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


# ==================== Управление монетами роботов ====================


def test_robot_coins_persistence_and_env_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RobotCoins: persist, env seed, явный пустой список, лимит."""
    import dashboard.robot_coins as coins_module
    from dashboard.robot_coins import RobotCoins

    coins_file = tmp_path / "robot_coins.json"
    monkeypatch.setattr(coins_module, "COINS_FILE", coins_file)
    monkeypatch.setattr(
        coins_module,
        "symbol_from_env",
        lambda name: "BTCUSDT" if name == "robot_trend" else "",
    )

    # Нет файла и нет .env → пусто; с сидом из .env → [BTCUSDT]
    assert RobotCoins.load().symbols_for("robot_flat") == []
    assert RobotCoins.load().symbols_for("robot_trend") == ["BTCUSDT"]

    coins = RobotCoins.load()
    coins.set("robot_trend", ["ethusdt", "ETHUSDT", "sol usdt", "SOLUSDT"])
    assert coins.symbols_for("robot_trend") == ["ETHUSDT", "SOLUSDT"]

    # Перезагрузка с диска: сохранённый список перекрывает сид
    reloaded = RobotCoins.load()
    assert reloaded.symbols_for("robot_trend") == ["ETHUSDT", "SOLUSDT"]

    # Явно пустой список — не откатывается на сид
    reloaded.set("robot_trend", [])
    assert RobotCoins.load().symbols_for("robot_trend") == []

    # Лимит монет на робота
    many = [f"S{i:03d}USDT" for i in range(coins_module.MAX_SYMBOLS_PER_ROBOT + 5)]
    assert len(reloaded.set("robot_trend", many)) == coins_module.MAX_SYMBOLS_PER_ROBOT


def test_robot_coins_reads_utf16_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Файл монет из PowerShell (UTF-16) и с BOM читается, а не роняет /api/robots."""
    import dashboard.robot_coins as coins_module
    from dashboard.robot_coins import RobotCoins

    coins_file = tmp_path / "robot_coins.json"
    monkeypatch.setattr(coins_module, "COINS_FILE", coins_file)

    coins_file.write_bytes('{"robot_trend": ["ENAUSDT"]}'.encode("utf-16"))
    assert RobotCoins.load().symbols_for("robot_trend") == ["ENAUSDT"]

    coins_file.write_bytes(json.dumps({"robot_trend": ["BTCUSDT"]}).encode("utf-8-sig"))
    assert RobotCoins.load().symbols_for("robot_trend") == ["BTCUSDT"]


def test_per_symbol_paths_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Имена файлов и окружение изолируются по монете."""
    from dashboard import robots as robots_module
    from dashboard.robots import _per_symbol_path, _symbol_env

    assert _per_symbol_path("data/trend_state.json", "robot_trend", "BTCUSDT") == str(
        Path("data/trend_state_BTCUSDT.json")
    )
    assert _per_symbol_path("", "robot_trend", "BTCUSDT") == ""

    monkeypatch.setattr(robots_module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "robot_trend").mkdir()
    (tmp_path / "robot_trend" / ".env").write_text(
        "SYMBOL=DOGEUSDT\nSTATE_FILE=data/state.json\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SYMBOL", "OTHERUSDT")

    env = _symbol_env("robot_trend", "SOLUSDT")
    assert env["SYMBOL"] == "SOLUSDT"
    assert env["STATE_FILE"] == str(Path("data/state_SOLUSDT.json"))
    assert env["LOG_FILE"] == str(Path("logs/robot_trend_SOLUSDT.log"))
    assert env["KILL_SWITCH"] == str(Path("data/robot_trend_SOLUSDT.kill"))


def test_spawn_runs_robot_as_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Робот стартует как модуль: при запуске скриптом падает импорт core."""
    from dashboard import robots as robots_module

    monkeypatch.setattr(robots_module, "PROJECT_ROOT", tmp_path)
    seen: list[tuple[list[str], str | None]] = []

    class FakeProc:
        pid = 4242

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        seen.append((list(argv), kwargs.get("cwd")))
        return FakeProc()

    monkeypatch.setattr(robots_module.subprocess, "Popen", fake_popen)
    pid = robots_module._spawn("robot_trend", "BTCUSDT")

    assert pid == 4242
    assert seen[0][0] == [sys.executable, "-m", "robot_trend.main"]
    assert seen[0][1] == str(tmp_path)
    assert (tmp_path / "logs" / "robot_trend_BTCUSDT.stderr.log").exists()


def test_get_robot_status_prunes_dead_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Мёртвый pid вычищается из реестра, а не роняет /api/robots (500)."""
    import dashboard.robot_coins as coins_module
    from dashboard import robots as robots_module

    monkeypatch.setattr(robots_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(robots_module, "_robots_cache", ["robot_trend"])
    monkeypatch.setattr(robots_module, "is_running", lambda pid: False)
    coins_file = tmp_path / "data" / "robot_coins.json"
    coins_file.parent.mkdir(parents=True)
    monkeypatch.setattr(coins_module, "COINS_FILE", coins_file)
    coins_file.write_text(json.dumps({"robot_trend": ["EVAAUSDT"]}), encoding="utf-8")
    robots_module._write_procs("robot_trend", {"EVAAUSDT": 9532})

    status = robots_module.get_robot_status("robot_trend")

    assert status.running is False
    assert status.symbol_pids == {}
    assert robots_module._read_procs("robot_trend") == {}


def test_robot_symbols_start_stop_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Старт по монетам, статус со symbol_pids, стоп, set_robot_symbols."""
    from dashboard import robots as robots_module
    from dashboard.robots import (
        get_robot_status,
        get_robot_symbols,
        set_robot_symbols,
        start_robot,
        stop_robot,
    )

    monkeypatch.setattr(robots_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(robots_module, "_robots_cache", ["robot_trend"])
    monkeypatch.setenv("DASH_ROBOT_START_WINDOW_SEC", "0")
    robot_dir = tmp_path / "robot_trend"
    robot_dir.mkdir()
    (robot_dir / "main.py").write_text("# stub\n", encoding="utf-8")
    coins_file = tmp_path / "data" / "robot_coins.json"
    coins_file.parent.mkdir()
    import dashboard.robot_coins as coins_module

    monkeypatch.setattr(coins_module, "COINS_FILE", coins_file)
    coins_file.write_text(
        json.dumps({"robot_trend": ["BTCUSDT", "ETHUSDT"]}), encoding="utf-8"
    )

    spawned: list[tuple[str, str]] = []
    pids = iter(range(100, 900))
    killed: list[int] = []

    def fake_spawn(robot: str, symbol: str) -> int:
        spawned.append((robot, symbol))
        return next(pids)

    monkeypatch.setattr(robots_module, "_spawn", fake_spawn)
    monkeypatch.setattr(robots_module, "is_running", lambda pid: pid not in killed)
    monkeypatch.setattr(robots_module, "_stop_symbol", _capture_stop(killed))

    # Старт: по процессу на каждую монету
    ok, message = start_robot("robot_trend")
    assert ok and "BTCUSDT" in message and "ETHUSDT" in message
    assert spawned == [("robot_trend", "BTCUSDT"), ("robot_trend", "ETHUSDT")]

    # Повторный старт — уже работает
    ok, message = start_robot("robot_trend")
    assert ok and "уже работает" in message

    # Статус: running + pids по монетам
    status = get_robot_status("robot_trend")
    assert status.running is True
    assert status.symbols == ["BTCUSDT", "ETHUSDT"]
    assert set(status.symbol_pids) == {"BTCUSDT", "ETHUSDT"}

    # set_robot_symbols: удаление останавливает процесс удалённой монеты
    ok, message = set_robot_symbols("robot_trend", ["ETHUSDT", "SOLUSDT"])
    assert ok and "BTCUSDT" in message
    assert get_robot_symbols("robot_trend") == ["ETHUSDT", "SOLUSDT"]
    assert killed == [status.symbol_pids["BTCUSDT"]]
    assert ("robot_trend", "SOLUSDT") in spawned

    # Стоп: все процессы и файлы реестра
    ok, message = stop_robot("robot_trend")
    assert ok and "ETHUSDT" in message
    assert not robots_module.procsfile_for("robot_trend").exists()
    assert get_robot_status("robot_trend").running is False

    # Старт без монет — отказ
    set_robot_symbols("robot_trend", [])
    ok, message = start_robot("robot_trend")
    assert not ok and "не выбраны" in message


def _capture_stop(killed: list[int]) -> Any:
    """Заглушка _stop_symbol: помечает pid убитым."""

    def stop(robot: str, symbol: str, procs: dict[str, int]) -> bool:
        pid = procs.pop(symbol, None)
        if pid is None:
            return False
        killed.append(pid)
        return True

    return stop


def _stagger_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbols: list[str]
) -> tuple[Any, list[tuple[str, str]], list[int]]:
    """Робот с монетами и заглушкой spawn; фоновый поток старта отключён."""
    import dashboard.robot_coins as coins_module
    from dashboard import robots as robots_module

    monkeypatch.setattr(robots_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(robots_module, "_robots_cache", ["robot_trend"])
    monkeypatch.setattr(robots_module, "start_stagger_worker", lambda: None)
    robot_dir = tmp_path / "robot_trend"
    robot_dir.mkdir()
    (robot_dir / "main.py").write_text("# stub\n", encoding="utf-8")
    coins_file = tmp_path / "data" / "robot_coins.json"
    coins_file.parent.mkdir()
    monkeypatch.setattr(coins_module, "COINS_FILE", coins_file)
    coins_file.write_text(json.dumps({"robot_trend": symbols}), encoding="utf-8")

    spawned: list[tuple[str, str]] = []
    killed: list[int] = []

    def fake_spawn(robot: str, symbol: str) -> int:
        spawned.append((robot, symbol))
        return 100 + len(spawned)

    monkeypatch.setattr(robots_module, "_spawn", fake_spawn)
    monkeypatch.setattr(robots_module, "is_running", lambda pid: pid not in killed)
    monkeypatch.setattr(robots_module, "_stop_symbol", _capture_stop(killed))
    return robots_module, spawned, killed


def test_start_robot_staggers_over_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Постепенный старт: первая монета сразу, остальные — по плану в окно."""
    robots, spawned, _killed = _stagger_fixture(
        tmp_path, monkeypatch, ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )
    monkeypatch.setenv("DASH_ROBOT_START_WINDOW_SEC", "600")

    ok, message = robots.start_robot("robot_trend")

    assert ok
    assert spawned == [("robot_trend", "BTCUSDT")]
    pending = robots._read_pending("robot_trend")
    assert set(pending) == {"ETHUSDT", "SOLUSDT"}
    assert all(at > time.time() for at in pending.values())
    assert "в течение 10 мин" in message

    ok, message = robots.start_robot("robot_trend")
    assert ok and "стартует" in message
    assert spawned == [("robot_trend", "BTCUSDT")]


def test_stagger_tick_starts_only_due_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тик стартует только созревшие монеты и снимает их с плана."""
    robots, spawned, _killed = _stagger_fixture(
        tmp_path, monkeypatch, ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )
    monkeypatch.setenv("DASH_ROBOT_START_WINDOW_SEC", "600")
    robots.start_robot("robot_trend")

    now = int(time.time())
    robots._write_pending("robot_trend", {"ETHUSDT": now - 1, "SOLUSDT": now + 3600})
    robots._stagger_tick()

    assert spawned == [("robot_trend", "BTCUSDT"), ("robot_trend", "ETHUSDT")]
    assert set(robots._read_pending("robot_trend")) == {"SOLUSDT"}

    robots._stagger_tick()
    assert len(spawned) == 2

    robots._write_pending("robot_trend", {"SOLUSDT": now - 1})
    robots._stagger_tick()
    assert len(spawned) == 3
    assert not robots.pendingfile_for("robot_trend").exists()


def test_added_symbol_starts_now_pending_stays_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Новая монета стартует сразу, а ждущие в плане не перезапускаются."""
    robots, spawned, _killed = _stagger_fixture(
        tmp_path, monkeypatch, ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    )
    monkeypatch.setenv("DASH_ROBOT_START_WINDOW_SEC", "600")
    robots.start_robot("robot_trend")

    ok, _msg = robots.set_robot_symbols(
        "robot_trend", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    )

    assert ok
    assert ("robot_trend", "XRPUSDT") in spawned
    assert ("robot_trend", "ETHUSDT") not in spawned
    assert set(robots._read_pending("robot_trend")) == {"ETHUSDT", "SOLUSDT"}

    ok, _msg = robots.set_robot_symbols("robot_trend", ["BTCUSDT", "XRPUSDT"])

    assert ok
    assert not robots.pendingfile_for("robot_trend").exists()
    assert ("robot_trend", "SOLUSDT") not in spawned


def test_symbols_cache_filter_sort_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SymbolsCache: фильтр USDT, порог оборота, сортировка, TTL."""
    from dashboard.symbols import SymbolsCache

    tickers = [
        {
            "symbol": "ETHUSDT",
            "lastPrice": "2000",
            "turnover24h": "3000000",
            "price24hPcnt": "-0.01",
        },
        {
            "symbol": "BTCUSDT",
            "lastPrice": "68000",
            "turnover24h": "9000000",
            "price24hPcnt": "0.02",
        },
        {"symbol": "SMALLUSDT", "turnover24h": "100"},  # ниже порога
        {"symbol": "FOOUSDC", "turnover24h": "9999999"},  # не USDT-M
        {"symbol": "BADUSDT", "turnover24h": "oops"},  # мусорный оборот
    ]

    class FakeFetcher:
        def __init__(self) -> None:
            self.calls = 0

        async def get_tickers(self) -> list[dict]:
            self.calls += 1
            return tickers

    fetcher = FakeFetcher()
    cache = SymbolsCache(fetcher=fetcher)  # type: ignore[arg-type]

    async def run() -> None:
        items = await cache.get()
        assert [i["symbol"] for i in items] == ["BTCUSDT", "ETHUSDT"]
        assert items[0]["turnover_24h"] == 9_000_000.0
        assert items[0]["change_pct"] == 2.0
        assert items[1]["last_price"] == 2000.0

        # TTL: кэш не бьёт в API повторно
        await cache.get()
        assert fetcher.calls == 1

        # force=True — обновление
        await cache.get(force=True)
        assert fetcher.calls == 2

    asyncio.run(run())


def test_symbols_api_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET/POST /api/robots/{name}/symbols и /api/bybit/symbols."""
    from dashboard import server as server_module

    monkeypatch.setattr(server_module, "get_all_robots_status", _fake_statuses)
    import dashboard.robot_coins as coins_module

    coins_file = tmp_path / "robot_coins.json"
    monkeypatch.setattr(coins_module, "COINS_FILE", coins_file)
    store: dict[str, list[str]] = {"robot_trend": ["BTCUSDT"]}
    monkeypatch.setattr(
        server_module, "get_robot_symbols", lambda name: list(store.get(name, []))
    )

    def fake_set(name: str, symbols: list[str]) -> tuple[bool, str]:
        store[name] = [s.upper() for s in symbols]
        return True, f"saved {len(symbols)}"

    monkeypatch.setattr(server_module, "set_robot_symbols", fake_set)

    async def run() -> None:
        resp = await server_module.api_robot_symbols_get(_path_request("robot_trend"))
        data = json.loads(bytes(resp.body))
        assert resp.status_code == 200
        assert data["symbols"] == ["BTCUSDT"]
        assert data["symbol_pids"] == {"ETHUSDT": 11}

        resp = await server_module.api_robot_symbols_get(_path_request("nope"))
        assert resp.status_code == 200
        assert json.loads(bytes(resp.body))["symbols"] == []

        resp = await server_module.api_robot_symbols_post(
            _path_request("robot_trend", {"symbols": ["solusdt", "BTCUSDT"]})
        )
        data = json.loads(bytes(resp.body))
        assert resp.status_code == 200
        assert data["symbols"] == ["SOLUSDT", "BTCUSDT"]

        resp = await server_module.api_robot_symbols_post(
            _path_request("robot_trend", {"add": ["dogeusdt"], "remove": ["btcusdt"]})
        )
        assert json.loads(bytes(resp.body))["symbols"] == ["SOLUSDT", "DOGEUSDT"]

        resp = await server_module.api_robot_symbols_post(
            _path_request("robot_trend", {"add": ["BAD SYMBOL!"]})
        )
        assert resp.status_code == 400

        resp = await server_module.api_robot_symbols_post(
            _path_request("robot_trend", None)
        )
        assert resp.status_code == 400

        class FakeCache:
            async def get(self, force: bool = False) -> list[dict[str, Any]]:
                return [{"symbol": "BTCUSDT", "turnover_24h": 1.0, "change_pct": 0.0}]

        request = _path_request("x")
        request.app.state.symbols_cache = FakeCache()
        resp = await server_module.api_bybit_symbols(request)
        data = json.loads(bytes(resp.body))
        assert resp.status_code == 200
        assert data["total"] == 1 and data["symbols"][0]["symbol"] == "BTCUSDT"

        resp = await server_module.api_robots_list(_path_request("x"))
        data = json.loads(bytes(resp.body))
        assert data["robots"][0]["symbols"] == ["BTCUSDT"]
        assert data["robots"][0]["symbol_pids"] == {"ETHUSDT": 11}

    asyncio.run(run())


def _fake_statuses() -> list[Any]:
    from dashboard.robots import RobotStatus

    return [
        RobotStatus(
            name="robot_trend",
            running=True,
            pid=11,
            symbols=["BTCUSDT"],
            symbol_pids={"ETHUSDT": 11},
        )
    ]


class _PathRequest:
    """Стаб Request с path_params/app.state/json()."""

    def __init__(self, name: str, body: dict[str, Any] | None = None) -> None:
        self.path_params = {"name": name}
        self._body = body
        self.app = SimpleNamespace(state=SimpleNamespace())
        self.query_params: dict[str, str] = {}

    async def json(self) -> dict[str, Any]:
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _path_request(name: str, body: dict[str, Any] | None = None) -> Any:
    return _PathRequest(name, body)


def test_scan_poc_returns_signal_from_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Адаптер yrovni превращает сигнал сканера в сигнал вкладки «Скринеры»."""
    spec = ScreenerSpec("yrovni", "Уровни POC", "bot_screener_yrovni", 300, 25)

    class FakeScanner:
        def __init__(self, config, symbol) -> None:
            self.symbol = symbol

        def refresh(self) -> POCSignal | None:
            if self.symbol != "AKEUSDT":
                return None
            return POCSignal(
                ts="2026-11-22T02:00:00+00:00",
                symbol="AKEUSDT",
                direction="long",
                entry_price=0.005219,
                price=0.005259,
                distance_pct=0.0076,
                zone_pct=0.10,
                signal_id="AKEUSDT:0.005219:long:1",
                members=[0.00521894],
                windows=["30d"],
                volume=4211.8,
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "bot_screener_yrovni.poc_scanner.scanner.POCScanner", FakeScanner
    )

    signals = asyncio.run(scan_poc(spec))

    assert len(signals) == 1
    signal = signals[0]
    assert signal.screener == "yrovni"
    assert signal.title == "Уровни POC"
    assert signal.symbol == "AKEUSDT"
    assert signal.side == "LONG"
    assert signal.price == pytest.approx(0.005219)
    assert signal.found_at == "2026-11-22T02:00:00+00:00"
    assert signal.detail["signal_id"] == "AKEUSDT:0.005219:long:1"
    assert signal.detail["windows"] == "30d"
    assert signal.detail["price"] == pytest.approx(0.005259)


def test_scan_poc_raises_when_every_symbol_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Если все символы недоступны — это сбой, а не «сигналов нет»."""
    spec = ScreenerSpec("yrovni", "Уровни POC", "bot_screener_yrovni", 300, 25)

    class FakeEnv:
        def names(self, key: str, default: tuple[str, ...] = ()) -> list[str]:
            return ["AAAUSDT", "BBBUSDT"]

    class FailingScanner:
        def __init__(self, config: object, symbol: str) -> None:
            self.symbol = symbol

        def refresh(self) -> POCSignal | None:
            raise RuntimeError("403")

        def close(self) -> None:
            pass

    monkeypatch.setattr("dashboard.adapters._env", lambda spec: FakeEnv())
    monkeypatch.setattr(
        "bot_screener_yrovni.poc_scanner.scanner.POCScanner", FailingScanner
    )

    with pytest.raises(RuntimeError, match="недоступны все символы"):
        asyncio.run(scan_poc(spec))


def test_poc_pending_signal_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending старше TTL помечается expired и не висит в «ожидают»."""
    import dashboard.poc_signals as poc_module

    monkeypatch.setattr(poc_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(poc_module, "POC_SIGNALS_FILE", tmp_path / "poc.json")
    monkeypatch.setattr(poc_module, "AUTO_TRADE_FILE", tmp_path / "auto.json")
    monkeypatch.setenv("POC_SIGNAL_TTL_MIN", "60")
    store = poc_module.PocSignalStore()
    record, created = store.add(
        {
            "signal_id": "AAAUSDT:1:long:1",
            "symbol": "AAAUSDT",
            "direction": "long",
            "entry_price": 1.0,
            "price": 1.01,
            "ts": "2026-11-22T00:00:00Z",
        }
    )

    assert created is True
    record.received_at = "2020-01-01T00:00:00+00:00"

    snapshot = store.snapshot()

    assert snapshot["pending"] == 0
    assert snapshot["signals"][0]["state"] == "expired"


def test_signals_clear_endpoint(store: SignalStore) -> None:
    """POST /api/signals/clear стирает сигналы и возвращает их число."""
    from dashboard.server import api_signals_clear

    store.set_result("impulse", [_signal("AAAUSDT", 1.0)], 1.0, "")
    fake_app = SimpleNamespace(state=SimpleNamespace(store=store))

    async def run() -> Any:
        request: Any = SimpleNamespace(app=fake_app)
        return await api_signals_clear(request)

    resp = asyncio.run(run())
    body = json.loads(resp.body)

    assert body == {"ok": True, "cleared": 1}
    assert store.snapshot()["total_signals"] == 0
