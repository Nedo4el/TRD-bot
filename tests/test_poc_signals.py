"""Тесты приёмника POC-сигналов: идемпотентность, подтверждение, AUTO_TRADE."""

from __future__ import annotations

from pathlib import Path

import pytest

from dashboard import poc_signals as module


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> module.PocSignalStore:
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "POC_SIGNALS_FILE", tmp_path / "poc_signals.json")
    monkeypatch.setattr(module, "AUTO_TRADE_FILE", tmp_path / "auto_trade.json")
    return module.PocSignalStore()


def payload(signal_id: str = "SOLUSDT:50:long:1") -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "symbol": "SOLUSDT",
        "direction": "long",
        "entry_price": 50.0,
        "price": 50.1,
        "distance_pct": 0.002,
        "zone_pct": 0.1,
        "ts": "2026-11-22T00:00:00+00:00",
    }


def test_duplicate_signal_id_is_not_stored_twice(
    store: module.PocSignalStore,
) -> None:
    first, created = store.add(payload())
    again, created_again = store.add(payload())

    assert created is True
    assert created_again is False
    assert again.signal_id == first.signal_id
    assert store.snapshot()["pending"] == 1


def test_confirm_moves_signal_to_active(store: module.PocSignalStore) -> None:
    record, _ = store.add(payload())

    confirmed = store.confirm(record.signal_id)

    assert confirmed is not None
    assert confirmed.state == module.STATE_ACTIVE
    assert confirmed.confirmed_at is not None
    assert store.snapshot()["active"] == 1


def test_auto_trade_makes_new_signal_active(
    store: module.PocSignalStore,
) -> None:
    module.AutoTradeFlag(auto_trade=True).save()

    record, _ = store.add(payload("SOLUSDT:50:long:2"))

    assert record.state == module.STATE_ACTIVE
    assert store.snapshot()["auto_trade"] is True


def test_signal_state_survives_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "POC_SIGNALS_FILE", tmp_path / "poc_signals.json")
    monkeypatch.setattr(module, "AUTO_TRADE_FILE", tmp_path / "auto_trade.json")
    first = module.PocSignalStore()
    first.add(payload())

    reopened = module.PocSignalStore()
    again, created = reopened.add(payload())

    assert created is False
    assert again.signal_id == payload()["signal_id"]
