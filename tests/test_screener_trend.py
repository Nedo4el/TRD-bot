"""Тесты скринера тренда: отбираем только тренд, вход — дело робота."""

from __future__ import annotations

import asyncio
import math
from typing import Any, cast

from bot_screener_trend.autostart import AutoStartConfig, pick_symbols
from bot_screener_trend.scanner import ScanConfig, ScanResult, analyze_symbol


def _candles(closes: list[float], volume: float = 1000.0) -> list[dict]:
    """Свечи из списка закрытий: open = предыдущий close, объём константный."""
    candles: list[dict] = []
    prev = closes[0]
    for i, close in enumerate(closes):
        candles.append(
            {
                "open_time": i * 60_000,
                "open": prev,
                "high": max(prev, close) + 0.1,
                "low": min(prev, close) - 0.1,
                "close": close,
                "volume": volume,
            }
        )
        prev = close
    return candles


def _rising(n: int = 250, step: float = 0.4) -> list[dict]:
    return _candles([100.0 + i * step for i in range(n)])


def _falling(n: int = 250, step: float = 0.4) -> list[dict]:
    return _candles([100.0 + i * step for i in range(n)][::-1])


def _flat(n: int = 250) -> list[dict]:
    """Синусоида вокруг 100: EMA переплетены, ADX низкий."""
    return _candles([100.0 + 0.5 * math.sin(i * math.pi / 10) for i in range(n)])


def _dip_last(candles: list[dict], volume: float = 5000.0) -> list[dict]:
    """Последняя свеча: откат к EMA_FAST с всплеском объёма."""
    prev = candles[-2]["close"]
    last = candles[-1]
    last.update(
        open=prev,
        close=prev,
        high=prev + 0.1,
        low=prev * 0.95,
        volume=volume,
    )
    return candles


def _htf_up() -> list[dict]:
    return _rising(step=1.0)


def _htf_down() -> list[dict]:
    return _falling(step=1.0)


_SCAN_ENV = {
    "timeframe": "5",
    "htf_timeframe": "60",
    "lookback_bars": 300,
    "htf_lookback_bars": 300,
}


class TestTrendDetection:
    def test_uptrend_detected(self) -> None:
        result = analyze_symbol(
            "TESTUSDT",
            _rising(),
            htf_candles=_htf_up(),
            turnover_24h=50_000_000,
        )
        assert result is not None
        assert result.trend == "UP"
        assert result.ema_fast > result.ema_slow
        assert result.adx_value >= ScanConfig().adx_threshold
        assert result.di_plus > result.di_minus
        assert result.htf_timeframe == "60"

    def test_downtrend_detected(self) -> None:
        result = analyze_symbol(
            "TESTUSDT", _falling(), htf_candles=_htf_down()
        )
        assert result is not None
        assert result.trend == "DOWN"
        assert result.ema_fast < result.ema_slow
        assert result.di_minus > result.di_plus

    def test_flat_market_no_signal(self) -> None:
        """Боковик: EMA переплетены, ADX низкий — сигнала нет."""
        assert analyze_symbol(
            "TESTUSDT", _rising(), htf_candles=_flat()
        ) is None

    def test_adx_gate_blocks(self) -> None:
        cfg = ScanConfig(adx_threshold=150.0)
        assert analyze_symbol(
            "TESTUSDT", _rising(), cfg=cfg, htf_candles=_htf_up()
        ) is None

    def test_insufficient_candles(self) -> None:
        cfg = ScanConfig()
        short = _rising()[: cfg.ema_macro + 1]
        assert analyze_symbol(
            "TESTUSDT", _rising(), cfg=cfg, htf_candles=short
        ) is None

    def test_trend_wins_over_local_move(self) -> None:
        """Тренд HTF восходящий, локально цена падает — сигнал всё равно есть."""
        result = analyze_symbol("TESTUSDT", _falling(), htf_candles=_htf_up())
        assert result is not None
        assert result.trend == "UP"

    def test_pullback_is_informational_only(self) -> None:
        """Откат отмечается флагом, но сигнал без него не пропадает."""
        no_pullback = analyze_symbol(
            "TESTUSDT", _rising(), htf_candles=_htf_up()
        )
        with_pullback = analyze_symbol(
            "TESTUSDT", _dip_last(_rising()), htf_candles=_htf_up()
        )
        assert no_pullback is not None and no_pullback.pullback is False
        assert with_pullback is not None and with_pullback.pullback is True

    def test_vol_ratio_reported(self) -> None:
        """Низкий объём — не повод отбросить монету, только цифра в таблице."""
        result = analyze_symbol(
            "TESTUSDT",
            _dip_last(_rising(), volume=100.0),
            htf_candles=_htf_up(),
        )
        assert result is not None
        assert result.vol_ratio < 1.0

    def test_empty_htf_returns_none(self) -> None:
        """Без HTF-свечей сигнала нет: старый режим по 5m удалён."""
        assert analyze_symbol("TESTUSDT", _rising(step=1.0), htf_candles=[]) is None


class TestScore:
    def test_score_formula(self) -> None:
        result = analyze_symbol("TESTUSDT", _rising(), htf_candles=_htf_up())
        assert result is not None
        expected = int(min(100.0, result.adx_value * 2.0 + result.spread_pct * 5.0))
        assert result.score == expected
        assert 0 <= result.score <= 100


def _result(symbol: str, score: int) -> ScanResult:
    return ScanResult(symbol=symbol, timeframe="5", price=1.0, trend="UP", score=score)


class TestAutoStart:
    def test_picks_strongest_first(self) -> None:
        results = [_result("AUSDT", 10), _result("BUSDT", 90), _result("CUSDT", 50)]
        assert pick_symbols(results, [], 2) == ["BUSDT", "CUSDT"]

    def test_skips_existing_symbols(self) -> None:
        results = [_result("AUSDT", 90), _result("BUSDT", 50)]
        assert pick_symbols(results, ["AUSDT"], 5) == ["BUSDT"]

    def test_respects_total_limit(self) -> None:
        results = [_result(f"{i}USDT", 100 - i) for i in range(5)]
        existing = [f"X{i}USDT" for i in range(19)]
        assert pick_symbols(results, existing, 3) == ["0USDT"]

    def test_disabled_by_default(self) -> None:
        cfg = AutoStartConfig()
        assert cfg.enabled is False
        assert cfg.robot_name == "robot_trend"


class TestScanOne:
    """Сетевой слой скринера: без HTF монета пропускается."""

    def test_missing_htf_skips_symbol(self) -> None:
        from bot_screener_trend.main import _scan_one

        class FakeFetcher:
            async def get_klines(
                self, symbol: str, interval: str, limit: int = 100
            ) -> list[dict]:
                return _rising() if interval == "5" else []

        result = asyncio.run(
            _scan_one(
                cast(Any, FakeFetcher()), "TESTUSDT", _SCAN_ENV, ScanConfig(), 1e9
            )
        )
        assert result is None

    def test_trend_measured_on_htf(self) -> None:
        from bot_screener_trend.main import _scan_one

        class FakeFetcher:
            async def get_klines(
                self, symbol: str, interval: str, limit: int = 100
            ) -> list[dict]:
                return _flat() if interval == "5" else _htf_up()

        result = asyncio.run(
            _scan_one(
                cast(Any, FakeFetcher()), "TESTUSDT", _SCAN_ENV, ScanConfig(), 1e9
            )
        )
        assert result is not None
        assert result.trend == "UP"
        assert result.htf_timeframe == "60"
