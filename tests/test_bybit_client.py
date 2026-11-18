"""Общий REST-лимит процессов и идемпотентность set_trading_stop."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
from pybit.exceptions import InvalidRequestError

import core.bybit_client as bc
from core.config import Config


def _gate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock = tmp_path / "gate.lock"
    monkeypatch.setattr(bc, "_GATE_LOCK", lock)
    monkeypatch.setattr(bc, "_GATE_TS", tmp_path / "gate.ts")
    return lock


def test_gate_keeps_shared_spacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _gate_paths(tmp_path, monkeypatch)
    assert bc._gate_delay(0.05) == 0.0
    started = time.monotonic()
    second = bc._gate_delay(0.05)
    assert second >= 0.03
    assert time.monotonic() - started < 0.5


def test_gate_recovers_from_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = _gate_paths(tmp_path, monkeypatch)
    lock.write_text("stale", encoding="ascii")
    old = time.time() - bc._GATE_STALE_SEC - 1
    os.utime(lock, (old, old))
    assert bc._gate_delay(0.01) == 0.0
    assert not lock.exists()


def test_call_treats_not_modified_as_success() -> None:
    client = bc.BybitClient(Config(api_key="k", api_secret="s", testnet=True))

    def already_set(**kwargs: Any) -> dict[str, Any]:
        raise InvalidRequestError(None, "not modified (ErrCode: 34040)", 400, 0, {})

    client._http.set_trading_stop = already_set  # type: ignore[method-assign]
    out = asyncio.run(client.set_stop_loss_take_profit("BTCUSDT", 95.0, 0))
    assert out["retCode"] == 0


def test_call_other_api_error_raises() -> None:
    client = bc.BybitClient(Config(api_key="k", api_secret="s", testnet=True))

    def bad_params(**kwargs: Any) -> dict[str, Any]:
        raise InvalidRequestError(None, "bad params (ErrCode: 10001)", 400, 0, {})

    client._http.set_trading_stop = bad_params  # type: ignore[method-assign]
    with pytest.raises(InvalidRequestError):
        asyncio.run(client.set_stop_loss_take_profit("BTCUSDT", 95.0))
