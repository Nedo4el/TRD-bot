"""Тесты POC-скринера: подтверждение объёмом, сигнал, слияние, малый объём."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bot_screener_yrovni.poc_scanner import klines
from bot_screener_yrovni.poc_scanner.config import POCConfig
from bot_screener_yrovni.poc_scanner.profile import compute_profile
from bot_screener_yrovni.poc_scanner.scanner import POCScanner
from bot_screener_yrovni.poc_scanner.signals import ACTION_PLACE
from bot_screener_yrovni.poc_scanner.store import LevelState
from bot_screener_yrovni.poc_scanner.timeutil import to_iso

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def make_config(tmp_path: Path) -> POCConfig:
    return POCConfig(
        symbol="SOLUSDT",
        db_path=tmp_path / "poc.sqlite",
        poc_log=tmp_path / "levels.jsonl",
        signal_log=tmp_path / "signals.jsonl",
    )


def make_scanner(tmp_path: Path) -> POCScanner:
    return POCScanner(make_config(tmp_path), "SOLUSDT")


def add_level(
    scanner: POCScanner,
    price: float,
    volume: float,
    windows: tuple[str, ...] = ("180d",),
    age_days: int = 180,
) -> LevelState:
    """Положить в хранилище уровень с заданной давностью и объёмом."""
    created = to_iso(NOW - timedelta(days=age_days))
    candidate = LevelState(
        symbol=scanner.symbol,
        poc_price=price,
        vah=price * 1.01,
        val=price * 0.99,
        source_windows=windows,
        window_volumes={name: volume / len(windows) for name in windows},
        first_created_ts=created,
        last_calculated_ts=created,
        va_window=windows[-1],
    )
    return scanner.store.upsert_level(
        candidate,
        merge_pct=scanner.config.merge_poc_pct,
        confirm_threshold=scanner.config.confirm_volume_threshold,
    )


def test_poc_confirmed_by_volume_without_touch(tmp_path: Path) -> None:
    """POC на $50 с объёмом $80M — confirmed, но касаний ещё не было."""
    scanner = make_scanner(tmp_path)

    level = add_level(scanner, 50.0, 80_000_000.0)

    assert level.confirmed is True
    assert level.touch_count == 0
    stored = scanner.store.levels("SOLUSDT")
    assert len(stored) == 1
    assert stored[0].touch_count == 0
    assert stored[0].total_volume_at_level == pytest.approx(80_000_000.0)


def test_price_returns_to_poc_generates_signal(tmp_path: Path) -> None:
    """Возврат цены к $50 даёт сигнал POC_REACHED с confidence > 0.6."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 80_000_000.0)

    signals = scanner.check_price_proximity(50.1, atr=0.5, now=NOW)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.confidence > 0.6
    assert signal.action == ACTION_PLACE
    assert signal.touch_count == 1
    assert signal.proximity_pct == pytest.approx(0.002, rel=0.05)
    assert signal.order_params["buy_at"] == pytest.approx(49.5)
    assert signal.order_params["sell_at"] == pytest.approx(50.5)
    assert signal.order_params["stop_buy"] < signal.order_params["buy_at"]
    assert (tmp_path / "signals.jsonl").exists()


def test_close_pocs_merge_into_one_level(tmp_path: Path) -> None:
    """POC $50 (7d) и $50.3 (30d) сливаются в один уровень."""
    scanner = make_scanner(tmp_path)

    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    add_level(scanner, 50.3, 40_000_000.0, windows=("30d",))

    levels = scanner.store.levels("SOLUSDT")

    assert len(levels) == 1
    merged = levels[0]
    assert merged.poc_price == pytest.approx(50.0)
    assert set(merged.source_windows) == {"7d", "30d"}
    assert merged.total_volume_at_level == pytest.approx(80_000_000.0)
    assert merged.confirmed is True


def test_small_volume_is_not_confirmed_and_gives_no_signal(
    tmp_path: Path,
) -> None:
    """Уровень с объёмом $5M не подтверждён и сигнала не даёт."""
    scanner = make_scanner(tmp_path)

    level = add_level(scanner, 50.0, 5_000_000.0)

    assert level.confirmed is False
    assert scanner.check_price_proximity(50.0, now=NOW) == []


def test_recalculation_does_not_inflate_volume(tmp_path: Path) -> None:
    """Повторный расчёт того же окна заменяет объём, а не копит его."""
    scanner = make_scanner(tmp_path)

    add_level(scanner, 50.0, 30_000_000.0, windows=("7d",))
    add_level(scanner, 50.0, 30_000_000.0, windows=("7d",))

    levels = scanner.store.levels("SOLUSDT")

    assert len(levels) == 1
    assert levels[0].total_volume_at_level == pytest.approx(30_000_000.0)
    assert levels[0].source_windows == ("7d",)


def test_signal_is_throttled_by_cooldown(tmp_path: Path) -> None:
    """Второй сигнал по тому же уровню в пределах кулдауна не выдаётся."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 80_000_000.0)

    first = scanner.check_price_proximity(50.0, now=NOW)
    second = scanner.check_price_proximity(
        50.0,
        now=NOW + timedelta(seconds=60),
    )

    assert len(first) == 1
    assert second == []


def test_windows_split_hourly_and_four_hour() -> None:
    """Окна до 30 дней считаются по 1h, длиннее — по 4h."""
    config = POCConfig(symbol="SOLUSDT")

    assert config.spec("30d").interval == "60"
    assert config.spec("30d").candles == 720
    assert config.spec("180d").interval == "240"
    assert config.spec("730d").candles == 4380
    assert config.days_for_interval("240") == 730


def test_profile_poc_lands_in_heaviest_zone() -> None:
    """POC профиля — там, где сконцентрирован объём."""
    frame = pd.DataFrame(
        [
            {
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
                "volume": 1.0,
                "turnover": 10.0,
            }
            for _ in range(5)
        ]
        + [
            {
                "open": 200.0,
                "high": 200.5,
                "low": 199.5,
                "close": 200.0,
                "volume": 1.0,
                "turnover": 10_000.0,
            }
            for _ in range(5)
        ]
    )

    result = compute_profile(frame, num_bins=100, va_percent=0.68)

    assert result is not None
    assert 199.0 <= result.poc <= 201.0
    assert result.val <= result.poc <= result.vah
    assert result.total_volume == pytest.approx(50_050.0)


def test_fetch_klines_covers_full_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пагинация назад покрывает весь диапазон, а не только свежий кусок."""
    step = klines.INTERVAL_MS["240"]
    total = 4380
    first_ts = 1_700_000_000_000
    times = [first_ts + index * step for index in range(total)]

    def fake_get_json(
        _url: str,
        params: dict[str, Any],
        _timeout: float,
    ) -> dict[str, Any]:
        start = int(params["start"])
        end = int(params["end"])
        limit = int(params["limit"])
        inside = [moment for moment in times if start <= moment <= end]
        rows = [
            [str(moment), "1", "2", "0.5", "1.5", "10", "15"]
            for moment in reversed(inside[-limit:])
        ]
        return {"retCode": 0, "result": {"list": rows}}

    monkeypatch.setattr(klines, "_get_json", fake_get_json)

    candles = klines.fetch_klines(
        "SOLUSDT",
        "240",
        times[0],
        times[-1],
        limiter=klines.RateLimiter(1000.0),
    )

    assert len(candles) == total
    assert candles[0]["open_time"] == times[0]
    assert candles[-1]["open_time"] == times[-1]
