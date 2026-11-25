"""Тесты POC-скринера: подтверждение объёмом, сигнал, слияние, малый объём."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from bot_screener_yrovni.poc_scanner import klines
from bot_screener_yrovni.poc_scanner.clusters import cluster_prices, round_level
from bot_screener_yrovni.poc_scanner.config import POCConfig, get_poc_age_days
from bot_screener_yrovni.poc_scanner.profile import compute_profile
from bot_screener_yrovni.poc_scanner.scanner import (
    POCScanner,
    departure_pct,
    level_touched_since,
    zone_contact_hours,
)
from bot_screener_yrovni.poc_scanner.store import LevelState
from bot_screener_yrovni.poc_scanner.timeutil import to_iso

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)


def make_config(
    tmp_path: Path,
    min_departure_pct: float = 0.0,
    touch_lookback_hours: float = 48.0,
) -> POCConfig:
    return POCConfig(
        symbol="SOLUSDT",
        db_path=tmp_path / "poc.sqlite",
        poc_log=tmp_path / "levels.jsonl",
        signal_log=tmp_path / "signals.jsonl",
        segment_mode="off",
        min_departure_pct=min_departure_pct,
        touch_lookback_hours=touch_lookback_hours,
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


def make_scanner(
    tmp_path: Path,
    min_departure_pct: float = 0.0,
    touch_lookback_hours: float = 48.0,
) -> POCScanner:
    config = make_config(tmp_path, min_departure_pct, touch_lookback_hours)
    return POCScanner(config, "SOLUSDT")


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
        if not isinstance(loaded, pd.DataFrame):
            loaded = pd.DataFrame()
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


def make_price_frame(
    prices: list[float],
    end: datetime = NOW,
    minutes: int = 5,
) -> pd.DataFrame:
    """Свечи цены с заданными закрытиями; последняя — в ``end``."""
    stamps = pd.date_range(
        end=end,
        periods=len(prices),
        freq=timedelta(minutes=minutes),
        tz="UTC",
    )
    return pd.DataFrame(
        {
            "ts": stamps,
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": 1.0,
            "turnover": 1_000.0,
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
    age_days: int | None = None,
) -> LevelState:
    """Положить в хранилище уровень с заданным объёмом и окнами."""
    age = (
        age_days
        if age_days is not None
        else max(get_poc_age_days(name) for name in windows)
    )
    created = to_iso(NOW - timedelta(days=age))
    candidate = LevelState(
        symbol=scanner.symbol,
        poc_price=price,
        vah=price * 1.01,
        val=price * 0.99,
        source_windows=windows,
        window_volumes={name: volume / len(windows) for name in windows},
        poc_age_days=age,
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
    """Возврат цены к $50 даёт сигнал long с входом на уровне кластера."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 80_000_000.0)

    signal = scanner.check_clusters(50.1, now=NOW)

    assert signal is not None
    assert signal.direction == "long"
    assert signal.entry_price == pytest.approx(50.0)
    assert signal.distance_pct == pytest.approx(0.002, rel=0.05)
    assert signal.signal_id == "SOLUSDT:50:long:1"
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


def test_small_volume_level_still_gives_signal(tmp_path: Path) -> None:
    """В модели «ближайший кластер» объём уровня не гейт: сигнал по зоне."""
    scanner = make_scanner(tmp_path)

    level = add_level(scanner, 50.0, 5_000_000.0)

    assert level.confirmed is False
    assert scanner.check_clusters(50.1, now=NOW) is not None


def test_recalculation_does_not_inflate_volume(tmp_path: Path) -> None:
    """Повторный расчёт того же окна заменяет объём, а не копит его."""
    scanner = make_scanner(tmp_path)

    add_level(scanner, 50.0, 30_000_000.0, windows=("7d",))
    add_level(scanner, 50.0, 30_000_000.0, windows=("7d",))

    levels = scanner.store.levels("SOLUSDT")

    assert len(levels) == 1
    assert levels[0].total_volume_at_level == pytest.approx(30_000_000.0)
    assert levels[0].source_windows == ("7d",)


def test_zone_signal_repeats_only_after_exit(tmp_path: Path) -> None:
    """Пока цена в зоне — один сигнал; после выхода и возврата — новый эпизод."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 80_000_000.0)

    first = scanner.check_clusters(50.1, now=NOW)
    second = scanner.check_clusters(50.1, now=NOW + timedelta(minutes=5))
    scanner.check_clusters(60.0, now=NOW + timedelta(minutes=10))
    third = scanner.check_clusters(50.1, now=NOW + timedelta(minutes=15))

    assert first is not None
    assert second is None
    assert third is not None
    assert third.signal_id != first.signal_id
    assert third.signal_id.endswith(":2")


def test_zone_state_survives_reopen(tmp_path: Path) -> None:
    """Эпизод зоны хранится в БД: после рестарта дубля нет."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 80_000_000.0)
    assert scanner.check_clusters(50.1, now=NOW) is not None
    scanner.close()

    reopened = POCScanner(make_config(tmp_path), "SOLUSDT")
    try:
        assert reopened.check_clusters(50.1, now=NOW + timedelta(minutes=5)) is None
    finally:
        reopened.close()


def test_zone_contact_hours_measures_run_and_quiet() -> None:
    """Функция считает время в зоне и паузу до текущего подхода."""
    frame = make_price_frame([50.5] * 3 + [60.0] * 10 + [50.5] * 3)

    quiet, contact = zone_contact_hours(frame, 50.0, 0.10, NOW)

    assert contact == pytest.approx(10 / 60)
    assert quiet == pytest.approx(55 / 60)


def test_zone_contact_hours_none_when_price_outside_zone() -> None:
    """Последняя свеча вне зоны — фильтру нечего проверять."""
    frame = make_price_frame([60.0] * 10)

    assert zone_contact_hours(frame, 50.0, 0.10, NOW) is None


def test_departure_pct_is_max_excursion_since_start() -> None:
    """Уход от уровня — максимум |цена − уровень| / уровень от начала окна."""
    frame = make_price_frame([100.0, 130.0, 80.0, 95.0], minutes=60)

    assert departure_pct(frame, 100.0, NOW - timedelta(hours=4)) == pytest.approx(0.30)
    assert departure_pct(frame, 100.0, NOW) == pytest.approx(0.05)


def test_level_touched_since_detects_wick() -> None:
    """Диапазон свечи накрыл уровень — касание; до окна и мимо — нет."""
    frame = make_price_frame([100.0, 100.0, 100.0], minutes=60)
    frame.loc[1, "low"] = 99.0

    assert level_touched_since(frame, 99.5, NOW - timedelta(hours=3))
    assert not level_touched_since(frame, 99.5, NOW)
    assert not level_touched_since(frame, 105.0, NOW - timedelta(hours=3))


def test_level_touched_recently_gives_no_signal(tmp_path: Path) -> None:
    """Цена проходила уровень 2 часа назад — сигнала нет."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0)
    frame = make_price_frame([60.0] * 864 + [50.5] * 2)
    frame.loc[840, "low"] = 49.5
    scanner._series["5"] = frame

    assert scanner.check_clusters(50.5, now=NOW) is None


def test_level_touched_before_lookback_gives_signal(tmp_path: Path) -> None:
    """Касание было 72 часа назад — вне окна 48ч, сигнал есть."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0)
    frame = make_price_frame([60.0] * 864 + [50.5] * 2)
    frame.loc[0, "low"] = 49.5
    scanner._series["5"] = frame

    signal = scanner.check_clusters(50.5, now=NOW)

    assert signal is not None
    assert signal.entry_price == pytest.approx(50.0)


def test_cluster_drops_touched_level(tmp_path: Path) -> None:
    """Уровень 50 касались недавно — сигнал строится по нетронутому 53."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    add_level(scanner, 53.0, 40_000_000.0, windows=("180d",))
    frame = make_price_frame([41.0] * 47 + [49.0] * 2, minutes=60)
    frame.loc[10, "high"] = 50.5
    scanner._series["60"] = frame

    signal = scanner.check_clusters(49.0, now=NOW)

    assert signal is not None
    assert signal.entry_price == pytest.approx(53.0)


def test_touch_filter_can_be_disabled(tmp_path: Path) -> None:
    """TOUCH_LOOKBACK_HOURS=0 — уровень с недавним касанием даёт сигнал."""
    scanner = make_scanner(tmp_path, touch_lookback_hours=0.0)
    add_level(scanner, 50.0, 40_000_000.0)
    frame = make_price_frame([60.0] * 864 + [50.5] * 2)
    frame.loc[840, "low"] = 49.5
    scanner._series["5"] = frame

    assert scanner.check_clusters(50.5, now=NOW) is not None


def test_level_without_departure_gives_no_signal(tmp_path: Path) -> None:
    """Цена не уходила от уровня на 20 % — это текущая зона, а не уровень."""
    scanner = make_scanner(tmp_path, min_departure_pct=0.20)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    scanner._series["60"] = make_price_frame(
        [50.5] * 2 + [56.0] * 25 + [50.5] * 2,
        minutes=60,
    )

    assert scanner.check_clusters(50.5, now=NOW) is None


def test_level_with_departure_gives_signal(tmp_path: Path) -> None:
    """Цена уходила от уровня на 22 % — уровень исторический, сигнал есть."""
    scanner = make_scanner(tmp_path, min_departure_pct=0.20)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    scanner._series["60"] = make_price_frame(
        [50.5] * 2 + [61.0] * 25 + [50.5] * 2,
        minutes=60,
    )

    signal = scanner.check_clusters(50.5, now=NOW)

    assert signal is not None
    assert signal.entry_price == pytest.approx(50.0)


def test_cluster_drops_level_without_departure(tmp_path: Path) -> None:
    """Свежий уровень 50 отброшен, сигнал строится по историческому 53."""
    scanner = make_scanner(tmp_path, min_departure_pct=0.20)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    add_level(scanner, 53.0, 40_000_000.0, windows=("180d",))
    scanner._series["60"] = make_price_frame([41.0] * 25 + [49.0] * 2, minutes=60)

    signal = scanner.check_clusters(49.0, now=NOW)

    assert signal is not None
    assert signal.entry_price == pytest.approx(53.0)


def test_signal_skipped_when_price_sits_in_zone_for_days(tmp_path: Path) -> None:
    """Цена торгуется у уровня двое суток — это не сигнал."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0)
    scanner._series["5"] = make_price_frame([50.5] * 576)

    assert scanner.check_clusters(50.5, now=NOW) is None


def test_signal_skipped_after_recent_contact(tmp_path: Path) -> None:
    """Подход был 4.75 часа назад — сигнал слабый, пропускаем."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0)
    prices = [60.0] * 800 + [50.5] * 6 + [60.0] * 56 + [50.5] * 2
    scanner._series["5"] = make_price_frame(prices)

    assert scanner.check_clusters(50.5, now=NOW) is None


def test_signal_allowed_after_long_absence(tmp_path: Path) -> None:
    """Цена вернулась к уровню после трёх суток вне зоны — сильный сигнал."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0)
    scanner._series["5"] = make_price_frame([60.0] * 864 + [50.5] * 2)

    signal = scanner.check_clusters(50.5, now=NOW)

    assert signal is not None
    assert signal.direction == "long"
    assert signal.entry_price == pytest.approx(50.0)


def test_only_nearest_cluster_is_monitored(tmp_path: Path) -> None:
    """Дальний кластер игнорируется, даже если цена ближе к нему, чем к другому."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    add_level(scanner, 90.0, 40_000_000.0, windows=("180d",))

    assert scanner.check_clusters(80.0, now=NOW) is None
    signal = scanner.check_clusters(88.0, now=NOW)

    assert signal is not None
    assert signal.direction == "short"
    assert signal.entry_price == pytest.approx(90.0)


def test_clusters_merge_transitively() -> None:
    """A~B и B~C склеивают три уровня; уровень кластера — среднее."""
    groups = cluster_prices([100.0, 104.5, 108.9], merge_pct=0.05)

    assert len(groups) == 1
    assert groups[0] == [100.0, 104.5, 108.9]
    assert round_level(sum(groups[0]) / 3) == pytest.approx(104.5)


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
        "730d",
    }
    assert level.poc_age_days == 30
    assert level.partial_window is True
    assert level.confirmed is True


def test_signal_uses_merged_level_age(tmp_path: Path) -> None:
    """Уровень из 7d и 30d проходит фильтр возраста (30 дней) и даёт сигнал."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    add_level(scanner, 50.3, 40_000_000.0, windows=("30d",))

    signal = scanner.check_clusters(50.1, now=NOW)

    assert signal is not None
    assert signal.entry_price == pytest.approx(50.0)


def test_young_level_gives_no_signal(tmp_path: Path) -> None:
    """Уровень только из 3d-окна моложе MIN_POC_AGE_DAYS и сигнала не даёт."""
    scanner = make_scanner(tmp_path)

    level = add_level(scanner, 50.0, 80_000_000.0, windows=("3d",))

    assert level.poc_age_days == 3
    assert scanner.check_clusters(50.0, now=NOW) is None


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
    assert set(level.source_windows) == {"730d"}
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
    assert scanner.check_clusters(60.0, now=NOW) is None


def test_price_for_zone_comes_from_closed_5m_candle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Цена сравнения — закрытая 5m свеча, а не 1h/4h (п.6)."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("2026-08",), age_days=40)
    frames = {
        "5": make_price_frame([60.0] * 286 + [50.5] * 2),
        "60": make_frame(days=30, price=60.0),
        "240": make_history(NOW - timedelta(days=30), NOW, price=60.0),
    }
    patch_history(scanner, monkeypatch, frames)

    signal = scanner.refresh(now=NOW)

    assert signal is not None
    assert signal.entry_price == pytest.approx(50.0)
    assert signal.price == pytest.approx(50.5)


def test_prune_removes_levels_of_removed_windows(tmp_path: Path) -> None:
    """После смены окон старые rolling-уровни удаляются, актуальные остаются."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("180d",))
    add_level(scanner, 60.0, 40_000_000.0, windows=("30d",))

    removed = scanner.store.prune_stale("SOLUSDT", {"30d"})

    assert removed == 1
    assert [level.poc_price for level in scanner.store.levels("SOLUSDT")] == [60.0]


def make_price_groups(groups: list[tuple[float, int, float]]) -> pd.DataFrame:
    """Часовые свечи: (цена, число свечей, оборот свечи) на каждую группу."""
    rows: list[dict[str, Any]] = []
    start = NOW - timedelta(days=7)
    for price, count, turnover in groups:
        for _ in range(count):
            rows.append(
                {
                    "ts": start + timedelta(hours=len(rows)),
                    "open": price,
                    "high": price + 0.05,
                    "low": price - 0.05,
                    "close": price,
                    "volume": 1.0,
                    "turnover": turnover,
                }
            )
    return pd.DataFrame(rows)


def test_profile_extracts_secondary_volume_nodes() -> None:
    """POC — плотнейшая корзина, прочие локальные максимумы — объёмные узлы."""
    frame = make_price_groups(
        [
            (100.0, 90, 3_000_000.0),
            (118.0, 60, 3_000_000.0),
            (137.0, 45, 3_000_000.0),
            (160.0, 15, 3_000_000.0),
        ]
    )

    result = compute_profile(frame, node_min_share=0.35, max_nodes=6)

    assert result is not None
    assert len(result.nodes) == 2
    prices = [node.price for node in result.nodes]
    assert min(prices) == pytest.approx(118.0, abs=1.0)
    assert max(prices) == pytest.approx(137.0, abs=1.0)
    assert result.nodes[0].share > result.nodes[1].share
    assert result.poc < 105.0


def test_profile_flat_frame_has_no_nodes() -> None:
    """Плоский профиль из одной цены узлов не даёт: нет концентрации объёма."""
    result = compute_profile(make_frame(30), node_min_share=0.35, max_nodes=6)

    assert result is not None
    assert result.nodes == ()


def test_profile_nodes_disabled_by_default() -> None:
    """Без node_min_share/max_nodes профиль работает как до появления узлов."""
    frame = make_price_groups([(100.0, 90, 3_000_000.0), (118.0, 60, 3_000_000.0)])

    result = compute_profile(frame)

    assert result is not None
    assert result.nodes == ()


def test_refresh_stores_secondary_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Второй по объёму узел окна сохраняется уровнем с меткой ``7d#2``."""
    config = POCConfig(
        symbol="SOLUSDT",
        windows=("7d",),
        db_path=tmp_path / "poc.sqlite",
        poc_log=tmp_path / "levels.jsonl",
        signal_log=tmp_path / "signals.jsonl",
        segment_mode="off",
    )
    scanner = POCScanner(config, "SOLUSDT")
    frame = make_price_groups(
        [
            (100.0, 90, 3_000_000.0),
            (118.0, 60, 3_000_000.0),
            (137.0, 18, 3_000_000.0),
        ]
    )
    patch_history(scanner, monkeypatch, frame)

    scanner.refresh(current_price=1000.0, now=NOW)

    levels = scanner.store.levels("SOLUSDT")
    labels = {name for level in levels for name in level.source_windows}
    node_levels = [level for level in levels if "7d#2" in level.source_windows]

    assert {"7d", "7d#2"} <= labels
    assert "7d#3" not in labels
    assert len(node_levels) == 1
    assert node_levels[0].poc_price == pytest.approx(118.0, abs=1.0)
    assert node_levels[0].poc_age_days == 7


def test_window_label_lives_on_single_level(tmp_path: Path) -> None:
    """Метка окна переезжает на новый уровень, старый без меток удаляется."""
    scanner = make_scanner(tmp_path)
    add_level(scanner, 50.0, 40_000_000.0, windows=("7d",))
    moved = add_level(scanner, 60.0, 40_000_000.0, windows=("7d",))

    levels = scanner.store.levels("SOLUSDT")

    assert [level.id for level in levels] == [moved.id]
    assert levels[0].source_windows == ("7d",)


def test_window_label_detach_keeps_other_windows(tmp_path: Path) -> None:
    """Уровень теряет только переехавшую метку: остальные окна и объём живут."""
    scanner = make_scanner(tmp_path)
    old = add_level(scanner, 50.0, 40_000_000.0, windows=("7d", "30d"))
    add_level(scanner, 60.0, 40_000_000.0, windows=("7d",))

    levels = scanner.store.levels("SOLUSDT")
    survivor = next(level for level in levels if level.id == old.id)

    assert survivor.source_windows == ("30d",)
    assert survivor.total_volume_at_level == pytest.approx(20_000_000.0)
    assert survivor.poc_age_days == 30


def test_duplicate_span_drops_stale_window_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Окно с тем же охватом истории не считается — его старая метка снимается."""
    config = POCConfig(
        symbol="SOLUSDT",
        windows=("180d", "365d", "730d"),
        db_path=tmp_path / "poc.sqlite",
        poc_log=tmp_path / "levels.jsonl",
        signal_log=tmp_path / "signals.jsonl",
        segment_mode="off",
    )
    scanner = POCScanner(config, "SOLUSDT")
    add_level(scanner, 50.0, 40_000_000.0, windows=("180d", "730d"))
    patch_history(scanner, monkeypatch, make_history(NOW - timedelta(days=128), NOW))

    scanner.refresh(current_price=1000.0, now=NOW)

    levels = scanner.store.levels("SOLUSDT")

    assert len(levels) == 1
    assert levels[0].source_windows == ("730d",)


def test_get_poc_age_days_ignores_node_rank() -> None:
    """Возраст узла — возраст его окна: ``30d#3`` -> 30."""
    assert get_poc_age_days("30d#3") == 30
    assert get_poc_age_days("730d#6") == 730
