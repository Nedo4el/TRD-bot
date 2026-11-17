"""Тесты PositionGuard: дивергенции, absorption, OI и приоритет действий."""

from __future__ import annotations

from typing import Any

import pytest

from core.bybit_client import Candle
from robot_trend.guard import (
    GuardAction,
    GuardParams,
    OiPoint,
    PositionGuard,
    partial_qty,
)

TF = 300_000


def _mk(i: int, o: float, h: float, l: float, c: float, v: float = 1000.0) -> Candle:
    return Candle(open_time=i * TF, open=o, high=h, low=l, close=c, volume=v)


def _flat(n: int, high: float = 100.0, low: float = 99.0, close: float = 99.5) -> list[Candle]:
    return [_mk(i, close, high, low, close) for i in range(n)]


def _guard(**overrides: Any) -> PositionGuard:
    params = GuardParams(**overrides)
    return PositionGuard(params)


# ==================== CVD ====================


def test_cvd_bearish_divergence_long_partial() -> None:
    candles = _flat(29) + [_mk(29, 99.8, 101.0, 99.7, 100.5)]
    cvd = [50.0] + [float(i) for i in range(1, 29)] + [29.0]
    verdict = _guard(cvd_window=30).check_cvd_divergence(candles, cvd, "long")
    assert verdict.action is GuardAction.EXIT_PARTIAL
    assert verdict.reason == "bearish_divergence"


def test_cvd_bearish_divergence_full_mode() -> None:
    candles = _flat(29) + [_mk(29, 99.8, 101.0, 99.7, 100.5)]
    cvd = [50.0] + [float(i) for i in range(1, 29)] + [29.0]
    verdict = _guard(cvd_window=30, cvd_exit_mode="full").check_cvd_divergence(
        candles, cvd, "long"
    )
    assert verdict.action is GuardAction.EXIT_FULL


def test_cvd_no_divergence_when_cvd_confirms() -> None:
    candles = _flat(29) + [_mk(29, 99.8, 101.0, 99.7, 100.5)]
    cvd = [float(i) for i in range(30)]
    verdict = _guard(cvd_window=30).check_cvd_divergence(candles, cvd, "long")
    assert verdict.action is GuardAction.HOLD


def test_cvd_bullish_divergence_short() -> None:
    candles = _flat(29) + [_mk(29, 99.5, 99.6, 98.0, 98.5)]
    cvd = [50.0] + [float(i) for i in range(1, 29)] + [60.0]
    verdict = _guard(cvd_window=30).check_cvd_divergence(candles, cvd, "short")
    assert verdict.action is GuardAction.EXIT_PARTIAL
    assert verdict.reason == "bullish_divergence"


def test_cvd_low_volume_gate() -> None:
    candles = _flat(29) + [_mk(29, 99.8, 101.0, 99.7, 100.5, v=1.0)]
    cvd = [50.0] + [float(i) for i in range(1, 29)] + [29.0]
    verdict = _guard(cvd_window=30).check_cvd_divergence(candles, cvd, "long")
    assert verdict.action is GuardAction.HOLD
    assert verdict.reason == "low_volume"


# ==================== absorption ====================


def _absorption_candles(
    *, exhausted: bool, body: float = 0.05, volume: float = 300.0
) -> list[Candle]:
    candles = [_mk(i, 100.0, 101.0, 99.0, 100.2, v=100.0) for i in range(34)]
    prev_low = 100.08 if exhausted else 99.0
    candles[33] = _mk(33, 100.0, 100.2, prev_low, 100.15, v=100.0)
    last_close = 100.0 if exhausted else 100.05
    candles.append(
        _mk(34, last_close - body, last_close + 0.05, last_close - body, last_close, v=volume)
    )
    return candles


def test_absorption_warning_long() -> None:
    verdict = _guard().check_absorption(_absorption_candles(exhausted=False), "long")
    assert verdict.action is GuardAction.WARN
    assert verdict.details["volume_ratio"] >= 2.0


def test_absorption_escalates_when_close_breaks_prev_low() -> None:
    verdict = _guard().check_absorption(_absorption_candles(exhausted=True), "long")
    assert verdict.action is GuardAction.EXIT_PARTIAL
    assert verdict.reason == "absorption_exhausted"


def test_absorption_absent_on_big_body() -> None:
    candles = _absorption_candles(exhausted=False)
    candles[-1] = _mk(34, 99.0, 102.2, 98.9, 102.0, v=300.0)
    verdict = _guard().check_absorption(candles, "long")
    assert verdict.action is GuardAction.HOLD
    assert verdict.reason == "no_absorption"


# ==================== OI ====================


def _oi_candles() -> list[Candle]:
    return [_mk(i, 98.0 + i * 0.05, 98.2 + i * 0.05, 97.9 + i * 0.05, 98.0 + i * 0.05) for i in range(30)]


def _oi_points(oi_now: float) -> list[OiPoint]:
    return [OiPoint(ts_ms=29 * TF, oi=1000.0), OiPoint(ts_ms=40 * TF, oi=oi_now)]


def test_oi_divergence_exit_partial() -> None:
    verdict = _guard().check_oi_divergence(
        _oi_candles(), _oi_points(900.0), 0.0, "long"
    )
    assert verdict.action is GuardAction.EXIT_PARTIAL
    assert verdict.reason == "oi_no_new_high"


def test_oi_no_divergence_when_oi_grows() -> None:
    verdict = _guard().check_oi_divergence(
        _oi_candles(), _oi_points(1010.0), 0.0, "long"
    )
    assert verdict.action is GuardAction.HOLD


def test_oi_crowded_funding_full_exit() -> None:
    verdict = _guard().check_oi_divergence(
        _oi_candles(), _oi_points(900.0), 0.001, "long"
    )
    assert verdict.action is GuardAction.EXIT_FULL
    assert verdict.reason == "oi_crowded"


def test_oi_short_mirror() -> None:
    candles = [_mk(i, 102.0 - i * 0.05, 102.1 - i * 0.05, 101.9 - i * 0.05, 102.0 - i * 0.05) for i in range(30)]
    verdict = _guard().check_oi_divergence(
        candles, [OiPoint(ts_ms=29 * TF, oi=1000.0), OiPoint(ts_ms=40 * TF, oi=900.0)], 0.0, "short"
    )
    assert verdict.action is GuardAction.EXIT_PARTIAL
    assert verdict.reason == "oi_no_new_low"


# ==================== общее ====================


def test_disabled_guard_holds() -> None:
    guard = _guard(enabled=False)
    candles = _flat(35)
    verdict = guard.evaluate(candles, [float(i) for i in range(35)], _oi_points(1.0), 0.01, "long")
    assert verdict.action is GuardAction.HOLD


def test_evaluate_picks_strongest_verdict() -> None:
    candles = [
        _mk(i, 98.0 + i * 0.05, 98.2 + i * 0.05, 97.9 + i * 0.05, 98.0 + i * 0.05, v=100.0)
        for i in range(35)
    ]
    candles[-1] = _mk(34, 99.65, 99.72, 99.6, 99.68, v=300.0)
    cvd = [float(i) for i in range(len(candles))]
    verdict = _guard(cvd_enabled=False).evaluate(
        candles, cvd, _oi_points(900.0), 0.001, "long"
    )
    assert verdict.action is GuardAction.EXIT_FULL
    assert verdict.module == "oi"


def test_partial_qty_rounds_down_to_step() -> None:
    assert partial_qty(10.0, 0.5, 0.1) == pytest.approx(5.0)
    assert partial_qty(1.005, 0.5, 0.01) == pytest.approx(0.5)
    assert partial_qty(0.03, 0.5, 0.01) == pytest.approx(0.01)
    assert partial_qty(0.0, 0.5, 0.01) == 0.0


def test_params_validation_rejects_bad_mode() -> None:
    with pytest.raises(ValueError):
        GuardParams(cvd_exit_mode="half")
    with pytest.raises(ValueError):
        GuardParams(partial_pct=0.0)

