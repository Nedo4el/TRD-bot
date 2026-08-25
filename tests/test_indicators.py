"""Тесты индикаторов (core/indicators.py) на известных значениях."""

from __future__ import annotations

from core.indicators import atr, bollinger, crossed_down, crossed_up, ema, rsi, sma


class TestSma:
    def test_known_values(self) -> None:
        # SMA3 по ряду 1..5: (1+2+3)/3, (2+3+4)/3, (3+4+5)/3
        assert sma([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [2.0, 3.0, 4.0]

    def test_not_enough_data(self) -> None:
        assert sma([1.0, 2.0], 5) == []

    def test_bad_period(self) -> None:
        assert sma([1.0, 2.0], 0) == []


class TestEma:
    def test_seed_is_sma(self) -> None:
        # Первое значение EMA = SMA за период
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = ema(values, 3)
        assert result[0] == 2.0

    def test_converges_to_constant(self) -> None:
        values = [10.0] * 50
        result = ema(values, 5)
        assert abs(result[-1] - 10.0) < 1e-9

    def test_not_enough_data(self) -> None:
        assert ema([1.0], 5) == []


class TestRsi:
    def test_all_rising_is_100(self) -> None:
        closes = [float(i) for i in range(1, 31)]
        assert rsi(closes, 14)[-1] == 100.0

    def test_all_falling_is_0(self) -> None:
        closes = [float(-i) for i in range(1, 31)]
        assert rsi(closes, 14)[-1] == 0.0

    def test_length(self) -> None:
        closes = [float(i) for i in range(30)]
        assert len(rsi(closes, 14)) == 30 - 14

    def test_in_range(self) -> None:
        pattern = [100, 101, 99, 102, 98, 103, 97, 100]
        closes = [float(c) for c in pattern * 5]
        assert all(0 <= v <= 100 for v in rsi(closes, 14))


class TestBollinger:
    def test_flat_series_bands_collapse(self) -> None:
        closes = [100.0] * 30
        upper, middle, lower = bollinger(closes, 20, 2.0)
        assert upper[-1] == middle[-1] == lower[-1] == 100.0

    def test_symmetry(self) -> None:
        closes = [float(100 + (i % 5) - 2) for i in range(30)]
        upper, middle, lower = bollinger(closes, 20, 2.0)
        assert abs((upper[-1] - middle[-1]) - (middle[-1] - lower[-1])) < 1e-9


class TestAtr:
    def test_constant_range(self) -> None:
        # Свечи с постоянным диапазоном 2.0 без гэпов -> ATR = 2.0
        highs = [101.0] * 30
        lows = [99.0] * 30
        closes = [100.0] * 30
        assert abs(atr(highs, lows, closes, 14)[-1] - 2.0) < 1e-9

    def test_length(self) -> None:
        n = 30
        # Первое значение появляется на свече с индексом period-1
        assert len(atr([1.0] * n, [0.0] * n, [0.5] * n, 14)) == n - 14 + 1

    def test_not_enough_data(self) -> None:
        assert atr([1.0] * 5, [0.0] * 5, [0.5] * 5, 14) == []


class TestCrossHelpers:
    def test_crossed_up(self) -> None:
        assert crossed_up(prev_a=1.0, prev_b=2.0, now_a=3.0, now_b=2.0)
        assert not crossed_up(prev_a=3.0, prev_b=2.0, now_a=1.0, now_b=2.0)

    def test_crossed_down(self) -> None:
        assert crossed_down(prev_a=2.0, prev_b=1.0, now_a=1.0, now_b=2.0)
        assert not crossed_down(prev_a=1.0, prev_b=2.0, now_a=3.0, now_b=2.0)
