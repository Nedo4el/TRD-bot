"""Тесты режима скана POC: настройки, автоскан по таймеру, передача роботу."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dashboard import poc_signals as poc_module
from dashboard import scan_mode as mode_module
from dashboard import scheduler as scheduler_module
from dashboard import store as store_module
from dashboard.config import DashboardConfig, ScreenerSpec
from dashboard.models import Signal, make_signal
from dashboard.poc_signals import PocSignalStore
from dashboard.scan_mode import INTERVALS, ScanModeSettings
from dashboard.scheduler import Scheduler, poc_payload
from dashboard.store import SignalStore

YROVNI = ScreenerSpec(
    name="yrovni",
    title="Уровни POC",
    dir_name="bot_screener_yrovni",
    interval_sec=300,
    max_symbols=100,
    live_signals=True,
)


@pytest.fixture
def mode_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mode_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mode_module, "SCAN_MODE_FILE", tmp_path / "scan_mode.json")
    return tmp_path / "scan_mode.json"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SignalStore:
    monkeypatch.setattr(store_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store_module, "STATE_FILE", tmp_path / "latest.json")
    return SignalStore([YROVNI])


@pytest.fixture
def poc_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PocSignalStore:
    monkeypatch.setattr(poc_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(poc_module, "POC_SIGNALS_FILE", tmp_path / "poc_signals.json")
    monkeypatch.setattr(poc_module, "AUTO_TRADE_FILE", tmp_path / "auto_trade.json")
    return PocSignalStore()


class FakeClock:
    """Ручные часы: таймер автоскана проверяется без реальных задержек."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _poc_signal(signal_id: str = "AKEUSDT:0.5:long:2") -> Signal:
    return make_signal(
        screener="yrovni",
        title="Уровни POC",
        symbol="AKEUSDT",
        side="long",
        price=0.5,
        score=3.0,
        label="POC",
        found_at="2026-11-23T09:00:00+00:00",
        detail={
            "signal_id": signal_id,
            "price": 0.502,
            "distance_pct": 0.004,
            "zone_pct": 0.01,
            "windows": "1h,4h",
            "volume": 12345.0,
        },
    )


def test_missing_file_falls_back_to_manual_mode(mode_file: Path) -> None:
    settings = ScanModeSettings.load()

    assert settings.mode == "manual"
    assert settings.interval_sec == 600
    assert settings.interval_sec in INTERVALS
    assert not mode_file.exists()


def test_broken_file_falls_back_to_manual_mode(mode_file: Path) -> None:
    mode_file.write_text("{not json", encoding="utf-8")

    assert ScanModeSettings.load() == ScanModeSettings()


def test_from_dict_rejects_unknown_mode_and_interval() -> None:
    settings = ScanModeSettings.from_dict({"mode": "turbo", "interval_sec": 15})

    assert settings.mode == "manual"
    assert settings.interval_sec == 600

    accepted = ScanModeSettings.from_dict({"mode": "AUTO", "interval_sec": 1800})
    assert accepted.mode == "auto"
    assert accepted.interval_sec == 1800


def test_save_round_trip(mode_file: Path) -> None:
    ScanModeSettings(mode="auto", interval_sec=60).save()

    raw = json.loads(mode_file.read_text(encoding="utf-8"))
    assert raw == {"mode": "auto", "interval_sec": 60}
    assert ScanModeSettings.load().to_dict() == raw


def test_scan_mode_api_round_trip(mode_file: Path) -> None:
    from dashboard.server import api_scan_mode_get, api_scan_mode_post

    async def run() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        request: Any = SimpleNamespace()
        current_response: Any = await api_scan_mode_get(request)
        current = json.loads(current_response.body)

        async def auto_json() -> dict[str, Any]:
            return {"mode": "auto", "interval_sec": 3600}

        post_request: Any = SimpleNamespace(json=auto_json)
        posted_response: Any = await api_scan_mode_post(post_request)
        posted = json.loads(posted_response.body)
        stored_response: Any = await api_scan_mode_get(request)
        stored = json.loads(stored_response.body)
        return current, posted, stored

    current, posted, stored = asyncio.run(run())

    assert current["ok"] is True
    assert current["mode"] == "manual"
    assert current["intervals"] == [60, 600, 1800, 3600]
    assert current["screener"] == "yrovni"
    assert posted["mode"] == "auto"
    assert posted["interval_sec"] == 3600
    assert stored["mode"] == "auto"
    assert stored["interval_sec"] == 3600


def test_scan_mode_api_rejects_non_object(mode_file: Path) -> None:
    from dashboard.server import api_scan_mode_post

    async def bad_json() -> list[Any]:
        return []

    request: Any = SimpleNamespace(json=bad_json)
    response = asyncio.run(api_scan_mode_post(request))

    assert response.status_code == 400


def test_auto_loop_scans_strictly_by_interval(
    store: SignalStore,
    poc_store: PocSignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    scans: list[float] = []
    second_scan = asyncio.Event()

    async def fake_scan(spec: ScreenerSpec) -> list[Signal]:
        scans.append(clock.now)
        if len(scans) == 2:
            second_scan.set()
        return []

    monkeypatch.setitem(scheduler_module.ADAPTERS, "yrovni", fake_scan)
    settings = ScanModeSettings(mode="auto", interval_sec=600)
    scheduler = Scheduler(
        [YROVNI],
        store,
        DashboardConfig(enabled=("yrovni",)),
        poc_store,
        poll_sec=0.01,
        mode_loader=lambda: settings,
        clock=clock,
    )

    async def run() -> list[float]:
        scheduler.start()
        try:
            await _wait_until(lambda: len(scans) == 1)
            clock.advance(599)
            await asyncio.sleep(0.05)
            assert len(scans) == 1, "скан раньше интервала"
            clock.advance(2)
            await asyncio.wait_for(second_scan.wait(), timeout=2)
        finally:
            await scheduler.stop()
        return scans

    measured = asyncio.run(run())

    assert measured == [1000.0, 1601.0]


def test_manual_mode_ignores_elapsed_time_until_switched_to_auto(
    store: SignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    scans: list[int] = []
    settings = ScanModeSettings(mode="manual", interval_sec=60)

    async def fake_scan(spec: ScreenerSpec) -> list[Signal]:
        scans.append(1)
        return []

    monkeypatch.setitem(scheduler_module.ADAPTERS, "yrovni", fake_scan)
    scheduler = Scheduler(
        [YROVNI],
        store,
        DashboardConfig(enabled=("yrovni",)),
        poll_sec=0.01,
        mode_loader=lambda: settings,
        clock=clock,
    )

    async def run() -> None:
        scheduler.start()
        try:
            clock.advance(7200)
            await asyncio.sleep(0.05)
            assert scans == [], "ручной режим не должен сканировать сам"
            settings.mode = "auto"
            await _wait_until(lambda: len(scans) == 1)
        finally:
            await scheduler.stop()

    asyncio.run(run())

    assert len(scans) == 1


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    """Дождаться условия, не привязываясь к фиксированной задержке."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("условие не выполнено за отведённое время")
        await asyncio.sleep(0.005)


def test_run_feeds_found_poc_signals_as_pending(
    store: SignalStore,
    poc_store: PocSignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_scan(spec: ScreenerSpec) -> list[Signal]:
        return [_poc_signal()]

    monkeypatch.setitem(scheduler_module.ADAPTERS, "yrovni", fake_scan)
    scheduler = Scheduler(
        [YROVNI],
        store,
        DashboardConfig(enabled=("yrovni",)),
        poc_store,
    )

    asyncio.run(scheduler._run(YROVNI))

    snapshot = poc_store.snapshot()
    assert snapshot["pending"] == 1
    record = snapshot["signals"][0]
    assert record["state"] == "pending"
    assert record["symbol"] == "AKEUSDT"
    assert record["direction"] == "long"
    assert record["entry_price"] == pytest.approx(0.5)
    assert record["windows"] == ["1h", "4h"]


def test_run_feeds_active_signal_when_auto_trade_enabled(
    store: SignalStore,
    poc_store: PocSignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poc_module.AutoTradeFlag(auto_trade=True).save()

    async def fake_scan(spec: ScreenerSpec) -> list[Signal]:
        return [_poc_signal()]

    monkeypatch.setitem(scheduler_module.ADAPTERS, "yrovni", fake_scan)
    scheduler = Scheduler(
        [YROVNI],
        store,
        DashboardConfig(enabled=("yrovni",)),
        poc_store,
    )

    asyncio.run(scheduler._run(YROVNI))

    snapshot = poc_store.snapshot()
    assert snapshot["active"] == 1
    assert snapshot["signals"][0]["state"] == "active"


def test_run_does_not_feed_non_live_screener(
    store: SignalStore,
    poc_store: PocSignalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = ScreenerSpec(
        name="all",
        title="Все монеты",
        dir_name="all",
        interval_sec=60,
        max_symbols=100,
    )

    async def fake_scan(target: ScreenerSpec) -> list[Signal]:
        return [_poc_signal()]

    monkeypatch.setitem(scheduler_module.ADAPTERS, "all", fake_scan)
    scheduler = Scheduler(
        [spec],
        store,
        DashboardConfig(enabled=("all",)),
        poc_store,
    )

    asyncio.run(scheduler._run(spec))

    assert poc_store.snapshot()["signals"] == []


def test_poc_payload_skips_signals_without_poc_id() -> None:
    plain = make_signal(
        screener="all",
        title="Все монеты",
        symbol="BTCUSDT",
        side="LONG",
        price=90000.0,
    )

    assert poc_payload(plain) is None


def test_poc_payload_matches_robot_contract() -> None:
    from robot_yrovni_D.signals import parse_signal

    payload = poc_payload(_poc_signal())

    assert payload is not None
    parsed = parse_signal(payload)
    assert parsed.signal_id == "AKEUSDT:0.5:long:2"
    assert parsed.symbol == "AKEUSDT"
    assert parsed.side_hint == "long"
    assert parsed.side == "Buy"
    assert parsed.poc_price == pytest.approx(0.5)
    assert parsed.current_price == pytest.approx(0.502)
    assert parsed.windows == ("1h", "4h")
    assert parsed.volume == pytest.approx(12345.0)
