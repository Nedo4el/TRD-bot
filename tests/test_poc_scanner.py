"""Тесты POC-скринера: подтверждение объёмом, сигнал, слияние, малый объём."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bot_screener_yrovni.poc_scanner import klines
from bot_screener_yrovni.poc_scanner.config import POCConfig, get_poc_age_days
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
        segment_mode="off",
    )


def make_segment_config(
    tmp_path: Path,
    windows: tuple[str, ...] = (),
    mode: str = "calendar_month",
) -> POCConfig:
    return POCConfig(
        symbol="SOLUSDT",
        windows=windows,
        db_path=tmp_path / "poc.sqlite",
        poc_log=tmp_path / "levels.jsonl",
        signal_log=tmp_path / "signals.jsonl",
        segment_mode=mode,
    )


def make_scanner(tmp_path: Path) -> POCScanner:
    return POCScanner(make_config(tmp_path), "SOLUSDT")


def make_frame(
    days: int,
    price: float = 100.0,
    turnover: float = 1_000_000.0,
) -> pd.DataFrame:
    """Часовые свечи за ``days`` дней вокруг одной цены."""
    stamps = pd.date_range(
        NOW - timedelta(days=days),
        periods=days * 24,
        freq=timedelta(hours=1),
        tz="UTC",
    )
    return pd.DataFrame(
        {
            "ts": stamps,
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "volume": 1.0,
            "turnover": turnover,
        }
    )


def patch_history(
    scanner: POCScanner,
    monkeypatch: pytest.MonkeyPatch,
    frame: pd.DataFrame | dict[str, pd.DataFrame],
) -> None:
    """Подменить загрузку истории: один кадр на все интервалы или по интервалам."""
    frames = frame if isinstance(frame, dict) else {}

    def fake_load(
        interval: str,
        now: datetime | None = None,
        days: int | None = None,
    ) -> pd.DataFrame:
        loaded = frames.get(interval) if frames else frame
        assert isinstance(loaded, pd.DataFrame)
        scanner._series[interval] = loaded
        return loaded

    monkeypatch.setattr(scanner, "load_history", fake_load)


def make_history(
    start: datetime,
    end: datetime,
    price: float = 50.0,
    turnover: float = 1_000_000.0,
    hours: int = 4,
) -> pd.DataFrame:
    """Четырёхчасовые свечи за период вокруг одной цены."""
    stamps = pd.date_range(start, end, freq=timedelta(hours=hours), tz="UTC")
    return pd.DataFrame(
        {
            "ts": stamps,
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "volume": 1.0,
            "turnover": turnover,
        }
    )


def set_month(
    frame: pd.DataFrame,
    month: str,
    price: float,
    turnover: float,
) -> pd.DataFrame:
    """Перенести свечи одного месяца на другую цену и оборот."""
    mask = frame["ts"].dt.strftime("%Y-%m") == month
    frame.loc[mask, ["open", "close"]] = price
    frame.loc[mask, "high"] = price + 0.05
    frame.loc[mask, "low"] = price - 0.05
    frame.loc[mask, "turnover"] = turnover
    return frame


def add_level(
    scanner: POCScanner,
    price: float,
    volume: float,
    windows: tuple[str, ...] = ("180d",),
) -> LevelState:
    """Положить в хранилище уровень с заданным объёмом и окнами."""
    age_days = max(get_poc_age_days(name) for name in windows)
    created = to_iso(NOW - timedelta(days=age_days))
    candidate = LevelState(
        symbol=scanner.symbol,
        poc_price=price,
        vah=price * 1.01,
        val=price * 0.99,
        source_windows=windows,
        window_volumes={name: volume / len(windows) for name in windows},
        poc_age_days=age_days,
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


def test_new_symbol_skips_young_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """30 дней истории: 3d/5d отброшены, остальные окна считаются как partial."""
    scanner = make_scanner(tmp_path)
    patch_history(scanner, monkeypatch, make_frame(days=30))

    scanner.refresh(current_price=100.0, now=NOW)

    levels = scanner.store.levels("SOLUSDT")

    assert len(levels) == 1
    level = levels[0]
    assert set(level.source_windows) == {
        "7d",
        "14d",
        "30d",
        "180d",
        "365d",
        "730d",
    }
    assert level.poc_age_days == 30
    assert level.partial_window is True
    assert level.confirmed is True


def test_signal_uses_oldest_window_age(tmp_path: Path) -> None:
    """Уровень из 7d и 30d проходит фильтр, возраст = 30, сигнал есть."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    add_level(scanner, 50.3, 40_000_000.0, windows=("30d",))

    signals = scanner.check_price_proximity(50.1, now=NOW)

    assert len(signals) == 1
    assert signals[0].poc_age_days == 30
    assert signals[0].action == ACTION_PLACE


def test_young_level_gives_no_signal(tmp_path: Path) -> None:
    """Уровень только из 3d-окна моложе MIN_POC_AGE_DAYS и сигнала не даёт."""
    scanner = make_scanner(tmp_path)

    level = add_level(scanner, 50.0, 80_000_000.0, windows=("3d",))

    assert level.poc_age_days == 3
    assert scanner.check_price_proximity(50.0, now=NOW) == []


def test_first_created_ts_is_window_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """first_created_ts уровня — начало самого старого окна, а не дата записи."""
    scanner = make_scanner(tmp_path)
    frame = make_frame(days=30)
    patch_history(scanner, monkeypatch, frame)

    scanner.refresh(current_price=100.0, now=NOW)

    level = scanner.store.levels("SOLUSDT")[0]
    window_start = to_iso(frame["ts"].iloc[0].to_pydatetime())

    assert level.first_created_ts == window_start
    assert level.first_created_ts < to_iso(NOW - timedelta(days=29))


def test_migration_fills_age_from_windows(tmp_path: Path) -> None:
    """Старая БД без poc_age_days получает возраст из source_windows."""
    db_path = tmp_path / "poc.sqlite"
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d", "30d"))
    scanner.store.close()

    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE poc_levels DROP COLUMN poc_age_days")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    reopened = POCScanner(make_config(tmp_path), "SOLUSDT")

    level = reopened.store.levels("SOLUSDT")[0]
    assert level.poc_age_days == 30
    reopened.store.close()


def test_short_history_creates_partial_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """128 дней истории: окна 180d/365d/730d считаются как partial, возраст 128."""
    config = POCConfig(
        symbol="SOLUSDT",
        windows=("180d", "365d", "730d"),
        db_path=tmp_path / "poc.sqlite",
        poc_log=tmp_path / "levels.jsonl",
        signal_log=tmp_path / "signals.jsonl",
        segment_mode="off",
    )
    scanner = POCScanner(config, "SOLUSDT")
    frame = make_history(NOW - timedelta(days=128), NOW)
    patch_history(scanner, monkeypatch, frame)

    scanner.refresh(current_price=50.0, now=NOW)

    levels = scanner.store.levels("SOLUSDT")

    assert len(levels) == 1
    level = levels[0]
    assert set(level.source_windows) == {"180d", "365d", "730d"}
    assert level.partial_window is True
    assert level.poc_age_days == 128


def test_calendar_poc_created_for_september(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сентябрь даёт calendar_month уровень 33.27 с возрастом от конца месяца."""
    scanner = POCScanner(make_segment_config(tmp_path), "SOLUSDT")
    frame = make_history(datetime(2026, 7, 1, tzinfo=timezone.utc), NOW)
    set_month(frame, "2026-09", 33.27, 20_000_000.0)
    patch_history(scanner, monkeypatch, {"240": frame})

    scanner.refresh(current_price=50.0, now=NOW)

    levels = scanner.store.levels("SOLUSDT")
    september = next(item for item in levels if abs(item.poc_price - 33.27) < 0.1)
    september_end = frame.loc[frame["ts"].dt.strftime("%Y-%m") == "2026-09", "ts"].iloc[
        -1
    ]

    assert september.segment_type == "calendar_month"
    assert september.poc_age_days == (NOW - september_end.to_pydatetime()).days
    assert september.confirmed is True
    assert september.partial_window is False


def test_calendar_poc_merges_into_matching_rolling_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сентябрьский POC 52.90 совпал с rolling 30d — один уровень, объёмы суммой."""
    scanner = POCScanner(make_segment_config(tmp_path), "SOLUSDT")
    add_level(scanner, 52.90, 40_000_000.0, windows=("30d",))
    frame = make_history(datetime(2026, 7, 1, tzinfo=timezone.utc), NOW)
    set_month(frame, "2026-09", 52.90, 20_000_000.0)
    patch_history(scanner, monkeypatch, {"240": frame})

    scanner.refresh(current_price=52.90, now=NOW)

    levels = scanner.store.levels("SOLUSDT")
    close = [
        item
        for item in levels
        if abs(item.poc_price - 52.90) / 52.90 <= scanner.config.merge_poc_pct
    ]
    september_volume = float(
        frame.loc[frame["ts"].dt.strftime("%Y-%m") == "2026-09", "turnover"].sum()
    )

    assert len(close) == 1
    merged = close[0]
    assert merged.segment_type == "rolling"
    assert "2026-09" in merged.source_windows
    assert merged.total_volume_at_level == pytest.approx(
        40_000_000.0 + september_volume
    )


def test_unmatched_calendar_poc_becomes_separate_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сентябрьский POC 33.27 не совпал с rolling 52.90 — отдельный уровень."""
    scanner = POCScanner(make_segment_config(tmp_path), "SOLUSDT")
    rolling = add_level(scanner, 52.90, 40_000_000.0, windows=("30d",))
    frame = make_history(datetime(2026, 7, 1, tzinfo=timezone.utc), NOW)
    set_month(frame, "2026-09", 33.27, 20_000_000.0)
    patch_history(scanner, monkeypatch, {"240": frame})

    scanner.refresh(current_price=33.27, now=NOW)

    levels = scanner.store.levels("SOLUSDT")
    september = next(item for item in levels if abs(item.poc_price - 33.27) < 0.1)
    stored_rolling = next(item for item in levels if abs(item.poc_price - 52.90) < 0.1)

    assert september.segment_type == "calendar_month"
    assert september.id != rolling.id
    assert stored_rolling.segment_type == "rolling"


def test_young_calendar_level_is_filtered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Текущий месяц с концом 3 дня назад: уровень есть, сигнала нет."""
    scanner = POCScanner(make_segment_config(tmp_path), "SOLUSDT")
    frame = make_history(
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        NOW - timedelta(days=3),
    )
    set_month(frame, "2026-10", 50.0, 20_000_000.0)
    set_month(frame, "2026-11", 60.0, 20_000_000.0)
    patch_history(scanner, monkeypatch, {"240": frame})

    signal = scanner.refresh(current_price=60.0, now=NOW)

    levels = scanner.store.levels("SOLUSDT")
    november = next(item for item in levels if abs(item.poc_price - 60.0) < 0.1)

    assert november.segment_type == "calendar_month"
    assert november.poc_age_days == 3
    assert signal is None
    assert scanner.check_price_proximity(60.0, now=NOW) == []
