"""Тесты скринера уровней: окна POC, экстремумы, ATH/ATL, порог 10%."""

from __future__ import annotations

from typing import Any

import pytest

from bot_screener_yrovni.scanner import (
    EXTREME_WINDOWS,
    KIND_ATH,
    KIND_ATL,
    KIND_HIGH,
    KIND_LOW,
    KIND_POC,
    MIN_CANDLES,
    WINDOWS,
    build_volume_profile,
    find_poc,
    interval_limits,
    scan_symbol,
    windows_from_series,
)


def _candle(
    high: float,
    low: float,
    close: float,
    turnover: float = 1_000.0,
) -> dict[str, Any]:
    return {
        "open_time": 0,
        "open": close,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1.0,
        "turnover": turnover,
    }


def _flat_series(price: float, count: int) -> list[dict[str, Any]]:
    """Ряд из count свечей узко вокруг цены."""
    return [_candle(price * 1.001, price * 0.999, price) for _ in range(count)]


def test_windows_cover_requested_periods() -> None:
    assert list(WINDOWS) == ["12h", "24h", "3d", "7d", "14d", "1m", "3m", "6m", "1y"]
    assert set(EXTREME_WINDOWS) == {"1m", "3m", "6m", "1y"}


def test_interval_limits_cover_every_window() -> None:
    limits = interval_limits()

    assert limits["60"] == 72
    assert limits["240"] == 180
    assert limits["D"] == 365


def test_windows_from_series_slices_tail() -> None:
    series = {"60": _flat_series(100.0, 200), "D": _flat_series(100.0, 400)}

    windows = windows_from_series(series)

    assert len(windows["12h"]) == 12
    assert len(windows["3d"]) == 72
    assert len(windows["1y"]) == 365
    assert "7d" not in windows


def test_profile_requires_min_candles() -> None:
    assert build_volume_profile(_flat_series(100.0, MIN_CANDLES - 1)) == []


def test_poc_lands_in_heaviest_price_zone() -> None:
    candles = [
        _candle(100.5, 99.5, 100.0, turnover=10.0),
        _candle(200.5, 199.5, 200.0, turnover=10_000.0),
    ]

    poc = find_poc(candles, num_bins=10)

    assert poc is not None
    assert 199.0 <= poc[0] <= 201.0


def test_scan_reports_signed_distance() -> None:
    series = windows_from_series({"60": _flat_series(100.0, 72)})
    series["12h"] = series["12h"][:-1] + [_candle(101.0, 99.0, 100.0)]

    signal = scan_symbol("AAAUSDT", series, num_bins=20, proximity_pct=10.0)

    assert signal.current_price == pytest.approx(100.0)
    assert signal.levels
    for level in signal.levels:
        assert level.kind == KIND_POC
        assert abs(level.distance_pct) <= 10.0


def test_scan_filters_levels_beyond_proximity() -> None:
    series = {"12h": _flat_series(100.0, 12)}

    near = scan_symbol("AAAUSDT", series, num_bins=20, proximity_pct=10.0)
    far = scan_symbol("AAAUSDT", series, num_bins=20, proximity_pct=0.5)

    assert near.levels
    assert far.levels == []


def test_scan_marks_long_window_extremes_and_all_time() -> None:
    series = windows_from_series(
        {
            "60": _flat_series(100.0, 72),
            "240": [_candle(108.0, 92.0, 100.0) for _ in range(180)],
            "D": _flat_series(100.0, 365),
        }
    )

    signal = scan_symbol(
        "AAAUSDT",
        series,
        all_time=(130.0, 70.0),
        num_bins=20,
        proximity_pct=10.0,
    )

    kinds = {level.kind for level in signal.levels}
    assert KIND_POC in kinds
    assert KIND_HIGH in kinds
    assert KIND_LOW in kinds
    assert KIND_ATH not in kinds
    assert KIND_ATL not in kinds
    assert not [lv for lv in signal.levels if lv.kind == KIND_HIGH and lv.period == "12h"]


def test_scan_marks_ath_atl_when_price_is_close() -> None:
    series = {"12h": _flat_series(100.0, 12)}

    signal = scan_symbol(
        "AAAUSDT",
        series,
        all_time=(105.0, 96.0),
        num_bins=20,
        proximity_pct=10.0,
    )

    by_kind = {level.kind: level for level in signal.levels}
    assert by_kind[KIND_ATH].period == "all"
    assert by_kind[KIND_ATH].distance_pct == pytest.approx(5.0)
    assert by_kind[KIND_ATL].distance_pct == pytest.approx(-4.0)


def test_scan_sorts_by_absolute_distance() -> None:
    series = windows_from_series({"60": _flat_series(100.0, 72)})

    signal = scan_symbol(
        "AAAUSDT",
        series,
        all_time=(109.0, 91.0),
        num_bins=20,
        proximity_pct=10.0,
    )

    distances = [abs(level.distance_pct) for level in signal.levels]
    assert distances == sorted(distances)


def test_scan_respects_max_price_filter() -> None:
    series = {"12h": _flat_series(100.0, 12)}

    signal = scan_symbol("AAAUSDT", series, max_price=50.0)

    assert signal.current_price == 0.0
    assert signal.levels == []
