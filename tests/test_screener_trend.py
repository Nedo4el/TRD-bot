"""Тесты скринера тренда (EMA + ADX, логика robot_trend)."""

from __future__ import annotations

from bot_screener_trend.scanner import ScanConfig, analyze_symbol


def _candles(closes: list[float]) -> list[dict]:
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
                "volume": 1000.0,
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
    import math

    closes = [100.0 + 0.5 * math.sin(i * math.pi / 10) for i in range(n)]
    return _candles(closes)


class TestTrendDetection:
    def test_uptrend_detected(self) -> None:
        result = analyze_symbol("TESTUSDT", _rising(), timeframe="1")
        assert result is not None
        assert result.trend == "UP"
        assert result.ema_fast > result.ema_slow
        assert result.price > result.ema_macro
        assert result.adx_value >= ScanConfig().adx_threshold
        assert result.di_plus > result.di_minus

    def test_downtrend_detected(self) -> None:
        result = analyze_symbol("TESTUSDT", _falling(), timeframe="1")
        assert result is not None
        assert result.trend == "DOWN"
        assert result.ema_fast < result.ema_slow
        assert result.price < result.ema_macro
        assert result.adx_value >= ScanConfig().adx_threshold
        assert result.di_minus > result.di_plus

    def test_flat_market_no_signal(self) -> None:
        assert analyze_symbol("TESTUSDT", _flat(), timeframe="1") is None

    def test_adx_gate_blocks(self) -> None:
        """Гейт ADX: порог выше фактического ADX — сигнала нет."""
        cfg = ScanConfig(adx_threshold=150.0)
        assert analyze_symbol("TESTUSDT", _rising(), cfg=cfg) is None

    def test_insufficient_candles(self) -> None:
        cfg = ScanConfig()
        short = _rising()[: cfg.ema_macro + 1]
        assert analyze_symbol("TESTUSDT", short, cfg=cfg) is None

    def test_pullback_below_ema_fast_no_signal(self) -> None:
        """Цена ниже EMA_FAST (откат), но выше EMA_MACRO — тренда нет."""
        candles = _rising()
        last = candles[-1]
        prev_close = candles[-2]["close"]
        last["open"] = prev_close
        last["close"] = prev_close * 0.90
        last["high"] = prev_close
        last["low"] = last["close"]
        result = analyze_symbol("TESTUSDT", candles)
        assert result is None


class TestScore:
    def test_score_formula(self) -> None:
        result = analyze_symbol("TESTUSDT", _rising(), timeframe="1")
        assert result is not None
        expected = int(min(100.0, result.adx_value * 2.0 + result.spread_pct * 5.0))
        assert result.score == expected
        assert 0 <= result.score <= 100

    def test_custom_config_respected(self) -> None:
        cfg = ScanConfig(adx_threshold=90.0)
        result = analyze_symbol("TESTUSDT", _rising(), cfg=cfg)
        # монотонный рост даёт ADX ~100, порог 90 всё ещё проходит
        assert result is not None
