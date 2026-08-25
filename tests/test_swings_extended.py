"""Углублённые тесты bot_swings: find_swings, пробои, ретесты, edge cases."""

from __future__ import annotations

import pytest

from bot_swings.strategy import SwingLevelsStrategy, find_swings
from core.bybit_client import Candle


def make_candles(rows: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(open_time=i * 60_000, open=o, high=h, low=lo, close=c, volume=1.0)
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def flat(price: float, count: int) -> list[tuple[float, float, float, float]]:
    return [(price, price + 0.1, price - 0.1, price)] * count


# ========================= find_swings =========================


class TestFindSwings:
    def test_resistance_found(self):
        rows = flat(100.0, 20)
        rows[10] = (100.0, 110.0, 99.0, 100.0)
        candles = make_candles(rows)
        resistances, supports = find_swings(candles, wing_size=3)
        assert any(abs(p - 110.0) < 1e-9 for _, p in resistances)

    def test_support_found(self):
        rows = flat(100.0, 20)
        rows[10] = (100.0, 101.0, 90.0, 100.0)
        candles = make_candles(rows)
        resistances, supports = find_swings(candles, wing_size=3)
        assert any(abs(p - 90.0) < 1e-9 for _, p in supports)

    def test_no_swings_on_flat(self):
        candles = make_candles(flat(100.0, 20))
        resistances, supports = find_swings(candles, wing_size=5)
        assert resistances == []
        assert supports == []

    def test_wing_size_too_large_returns_empty(self):
        rows = flat(100.0, 10)
        rows[5] = (100.0, 110.0, 90.0, 100.0)
        candles = make_candles(rows)
        resistances, supports = find_swings(candles, wing_size=10)
        assert resistances == []
        assert supports == []

    def test_equal_highs_not_resistance(self):
        rows = flat(100.0, 20)
        candles = make_candles(rows)
        resistances, supports = find_swings(candles, wing_size=3)
        assert resistances == []

    def test_multiple_levels(self):
        rows = flat(100.0, 30)
        rows[7] = (100.0, 112.0, 99.0, 100.0)
        rows[20] = (100.0, 115.0, 99.0, 100.0)
        candles = make_candles(rows)
        resistances, supports = find_swings(candles, wing_size=3)
        prices = [p for _, p in resistances]
        assert len(prices) >= 2


# ========================= SwingLevelsStrategy =========================

# need = lookback + wing_size + 3 — минимальное кол-во свечей


class TestSwingLevelsStrategyUnit:
    def test_insufficient_data(self):
        s = SwingLevelsStrategy(lookback=50, wing_size=5)
        signal = s.check_signal(make_candles(flat(100.0, 10)))
        assert signal.action == "hold"
        assert "недостаточно" in signal.reason.lower()

    def test_levels_updated_on_each_call(self):
        # need = 25 + 3 + 3 = 31; даём 35
        s = SwingLevelsStrategy(lookback=25, wing_size=3, max_levels=3)
        rows = flat(100.0, 35)
        rows[15] = (100.0, 115.0, 99.0, 100.0)
        candles = make_candles(rows)
        s.check_signal(candles)
        assert len(s.resistance_levels) > 0

    def test_max_levels_limits_stored(self):
        # need = 40 + 3 + 3 = 46; даём 50
        s = SwingLevelsStrategy(lookback=40, wing_size=3, max_levels=2)
        rows = flat(100.0, 50)
        rows[10] = (100.0, 112.0, 99.0, 100.0)
        rows[25] = (100.0, 115.0, 99.0, 100.0)
        rows[40] = (100.0, 118.0, 99.0, 100.0)
        candles = make_candles(rows)
        s.check_signal(candles)
        assert len(s.resistance_levels) <= 2

    def test_hold_when_no_setup(self):
        # need = 20 + 3 + 3 = 26; даём 30
        s = SwingLevelsStrategy(lookback=20, wing_size=3)
        candles = make_candles(flat(100.0, 30))
        signal = s.check_signal(candles)
        assert signal.action == "hold"
        assert "уровней в памяти" in signal.reason


# ========================= Breakout detection =========================

# need = lookback + wing_size + 3


class TestBreakoutDetection:
    def test_long_breakout_registers_setup(self):
        # need = 35 + 3 + 3 = 41; даём 42
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = flat(100.0, 42)
        rows[15] = (100.0, 110.0, 99.0, 100.0)  # уровень
        rows[40] = (100.0, 101.0, 99.0, 100.0)  # перед пробоем: close=100 < 110
        rows[41] = (100.0, 112.0, 100.5, 111.0)  # пробой вверх: close=111 > 110
        candles = make_candles(rows)
        s.check_signal(candles)
        assert s._long_setup is not None
        assert abs(s._long_setup["level"] - 110.0) < 1e-9

    def test_short_breakout_registers_setup(self):
        # need = 35 + 3 + 3 = 41; даём 42
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = flat(100.0, 42)
        rows[15] = (100.0, 101.0, 90.0, 100.0)  # уровень поддержки
        rows[40] = (100.0, 101.0, 99.0, 100.0)  # перед пробоем: close=100 > 90
        rows[41] = (100.0, 99.5, 88.0, 89.0)  # пробой вниз: close=89 < 90
        candles = make_candles(rows)
        s.check_signal(candles)
        assert s._short_setup is not None
        assert abs(s._short_setup["level"] - 90.0) < 1e-9

    def test_no_breakout_if_close_below_level(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3)
        rows = flat(100.0, 42)
        rows[15] = (100.0, 110.0, 99.0, 100.0)
        rows[40] = (100.0, 101.0, 99.0, 100.0)
        rows[41] = (100.0, 112.0, 100.5, 109.0)  # high пробил, но close под
        candles = make_candles(rows)
        s.check_signal(candles)
        assert s._long_setup is None


# ========================= Retest logic =========================

# need = lookback + wing_size + 3 = 35 + 3 + 3 = 41


class TestRetestLogic:
    def _make_breakout_scenario(self, level=110.0):
        # 42 свечи + потом ретест = 43
        rows = flat(100.0, 42)
        rows[15] = (100.0, level, 99.0, 100.0)
        rows[40] = (100.0, 101.0, 99.0, 100.0)
        rows[41] = (100.0, level + 2, level + 0.5, level + 1.5)
        return rows

    def test_retest_buy(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = self._make_breakout_scenario()
        candles = make_candles(rows)
        s.check_signal(candles)  # регистрирует breakout
        assert s._long_setup is not None

        # Ретест: касание уровня + бычье закрытие над ним
        retest = (109.5, 110.3, 109.3, 110.1)
        full = make_candles(rows + [retest])
        signal = s.check_signal(full)
        assert signal.action == "buy"
        assert "ретест" in signal.reason.lower()

    def test_retest_sell(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = flat(100.0, 42)
        rows[15] = (100.0, 101.0, 90.0, 100.0)
        rows[40] = (100.0, 101.0, 99.0, 100.0)
        rows[41] = (100.0, 89.5, 87.0, 88.0)
        candles = make_candles(rows)
        s.check_signal(candles)

        retest = (90.5, 90.8, 89.3, 89.5)
        full = make_candles(rows + [retest])
        signal = s.check_signal(full)
        assert signal.action == "sell"
        assert "ретест" in signal.reason.lower()

    def test_retest_timeout_clears_setup(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=3)
        rows = self._make_breakout_scenario()
        candles = make_candles(rows)
        s.check_signal(candles)
        assert s._long_setup is not None

        extra = flat(112.0, 5)
        full = make_candles(rows + extra)
        s.check_signal(full)
        assert s._long_setup is None

    def test_retest_bearish_candle_no_entry(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = self._make_breakout_scenario()
        candles = make_candles(rows)
        s.check_signal(candles)

        bearish = (110.5, 110.8, 109.3, 110.0)
        full = make_candles(rows + [bearish])
        signal = s.check_signal(full)
        assert signal.action == "hold"

    def test_retest_no_bounce_below_level_no_entry(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = self._make_breakout_scenario(level=110.0)
        candles = make_candles(rows)
        s.check_signal(candles)

        failed = (109.0, 110.3, 108.5, 109.0)
        full = make_candles(rows + [failed])
        signal = s.check_signal(full)
        assert signal.action == "hold"

    def test_no_retest_on_breakout_bar_itself(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = flat(100.0, 42)
        rows[15] = (100.0, 110.0, 99.0, 100.0)
        rows[40] = (100.0, 101.0, 99.0, 100.0)
        rows[41] = (100.0, 112.0, 109.8, 111.0)
        candles = make_candles(rows)
        signal = s.check_signal(candles)
        assert signal.action == "hold"
        assert s._long_setup is not None


# ========================= State management =========================


class TestStateManagement:
    def test_setup_cleared_after_signal(self):
        s = SwingLevelsStrategy(lookback=35, wing_size=3, retest_max_bars=20)
        rows = flat(100.0, 42)
        rows[15] = (100.0, 110.0, 99.0, 100.0)
        rows[40] = (100.0, 101.0, 99.0, 100.0)
        rows[41] = (100.0, 112.0, 100.5, 111.0)
        candles = make_candles(rows)
        s.check_signal(candles)

        retest = (109.5, 110.3, 109.3, 110.1)
        full = make_candles(rows + [retest])
        s.check_signal(full)
        assert s._long_setup is None

    def test_independent_long_short_setups(self):
        # need = 44 + 3 + 3 = 50; даём 52
        s = SwingLevelsStrategy(lookback=44, wing_size=3, retest_max_bars=20)
        rows = flat(100.0, 52)
        rows[10] = (100.0, 112.0, 99.0, 100.0)  # resistance
        rows[30] = (100.0, 101.0, 88.0, 100.0)  # support
        rows[50] = (100.0, 101.0, 99.0, 100.0)
        rows[51] = (100.0, 113.0, 86.0, 87.0)  # пробой support вниз (close=87 < 88)
        candles = make_candles(rows)
        s.check_signal(candles)
        assert s._short_setup is not None
