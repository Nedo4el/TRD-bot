"""Тесты стратегий на синтетических свечах (без сети).

Хелпер make_candles строит свечи из кортежей (open, high, low, close).
"""

from __future__ import annotations

from bot_adaptive.strategy import AdaptiveAtrStrategy
from bot_combo.strategy import ComboFilterStrategy
from bot_sma.strategy import SmaCrossStrategy
from bot_swings.strategy import SwingLevelsStrategy
from core.bybit_client import Candle
from core.indicators import atr


def make_candles(rows: list[tuple[float, float, float, float]]) -> list[Candle]:
    """Собрать свечи из (open, high, low, close), время — по порядку."""
    return [
        Candle(
            open_time=i * 60_000,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=1.0,
        )
        for i, (o, h, lo, c) in enumerate(rows)
    ]


def flat(price: float, count: int, jitter: float = 0.0) -> list:
    """Плоский участок: count одинаковых свечей с лёгким дрожанием."""
    rows = []
    for i in range(count):
        shift = jitter if i % 2 else -jitter
        p = price + shift
        rows.append((p - 0.01, p + 0.1, p - 0.1, p))
    return rows


# ============================ SMA crossover ============================


class TestSmaCrossStrategy:
    def test_golden_cross_gives_buy(self) -> None:
        # Падение, затем резкий разворот вверх -> где-то золотое пересечение
        closes_down = [(105 - i * 0.25) for i in range(40)]
        closes_up = [95 + i * 0.8 for i in range(15)]
        candles = make_candles(
            [(c, c + 0.2, c - 0.2, c) for c in closes_down + closes_up],
        )
        strategy = SmaCrossStrategy(fast_period=7, slow_period=25)

        actions = [
            strategy.check_signal(candles[: i + 1]).action
            for i in range(30, len(candles))
        ]
        assert "buy" in actions
        # На падающем участке (до разворота) покупок быть не должно
        assert "buy" not in actions[:10]

    def test_death_cross_gives_sell(self) -> None:
        closes_up = [(95 + i * 0.25) for i in range(40)]
        closes_down = [105 - i * 0.8 for i in range(15)]
        candles = make_candles(
            [(c, c + 0.2, c - 0.2, c) for c in closes_up + closes_down],
        )
        strategy = SmaCrossStrategy(fast_period=7, slow_period=25)

        actions = [
            strategy.check_signal(candles[: i + 1]).action
            for i in range(30, len(candles))
        ]
        assert "sell" in actions

    def test_insufficient_data_holds(self) -> None:
        strategy = SmaCrossStrategy(fast_period=7, slow_period=25)
        signal = strategy.check_signal(make_candles(flat(100.0, 10)))
        assert signal.action == "hold"

    def test_fast_must_be_less_than_slow(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="меньше"):
            SmaCrossStrategy(fast_period=25, slow_period=7)


# ========================= Комбинированный фильтр =========================


class TestComboFilterStrategy:
    def test_insufficient_data_returns_zero(self) -> None:
        strategy = ComboFilterStrategy()
        assert strategy.check_conditions(make_candles(flat(100.0, 30))) == 0

    def test_never_sells_in_strong_uptrend(self) -> None:
        # Плавный рост: short-сигналов быть не должно ни на одной свече
        closes = [100 + i * 0.5 for i in range(80)]
        candles = make_candles(
            [(c - 0.2, c + 0.3, c - 0.4, c) for c in closes],
        )
        strategy = ComboFilterStrategy()
        results = [strategy.check_conditions(candles[: i + 1]) for i in range(60, 80)]
        assert all(r != -1 for r in results)

    def test_never_buys_in_strong_downtrend(self) -> None:
        closes = [180 - i * 0.5 for i in range(80)]
        candles = make_candles(
            [(c + 0.4, c + 0.3, c - 0.2, c) for c in closes],
        )
        strategy = ComboFilterStrategy()
        results = [strategy.check_conditions(candles[: i + 1]) for i in range(60, 80)]
        assert all(r != 1 for r in results)

    def test_check_signal_maps_codes_to_actions(self) -> None:
        strategy = ComboFilterStrategy()
        candles = make_candles(flat(100.0, 100))
        signal = strategy.check_signal(candles)
        # На плоскости сигнала нет
        assert signal.action == "hold"


# ====================== Адаптивная стратегия ATR ======================


class TestAdaptiveAtrStrategy:
    def _sine_rows(
        self,
        count: int,
        amplitude: float,
        wick: float,
        phase: int = 0,
        center: float = 100.0,
    ) -> list:
        """Синусоида: гарантированные пересечения EMA + стабильный ATR.

        Wick (полуширина свечи) задаёт истинный диапазон 2*wick;
        он больше внутрисвечного движения цены, поэтому TR = high - low
        и ATR предсказуемы: сегмент с широкими свечами «шумнее».
        Параметр center сдвигает уровень ряда — так сегмент 1 «тянет»
        SMA200 вниз, и покупки в сегменте 2 разрешены трендом.
        """
        import math

        rows = []
        for i in range(count):
            base = center + amplitude * math.sin((i + phase) / 6)
            rows.append((base, base + wick, base - wick, base))
        return rows

    def test_insufficient_data_holds(self) -> None:
        strategy = AdaptiveAtrStrategy(trend_period=200)
        signal = strategy.check_signal(make_candles(flat(100.0, 50)))
        assert signal.action == "hold"

    def test_buy_signal_has_atr_stops_below_and_above(self) -> None:
        # Сегмент 1 (центр 95, широкие свечи ATR~3.0): поднимает средний ATR
        # и опускает SMA200 ниже диапазона сегмента 2 (99..101).
        # Итого: покупки разрешены трендом, фильтры волатильности проходят.
        strategy = AdaptiveAtrStrategy(
            trend_period=200,
            ema_fast=5,
            ema_slow=13,
            atr_period=14,
            min_atr_pct=0.05,
        )
        rows = self._sine_rows(150, amplitude=2.0, wick=1.5, center=95.0)
        rows += self._sine_rows(160, amplitude=1.0, wick=0.3, phase=150)
        candles = make_candles(rows)

        buy_window = None
        for i in range(230, len(candles)):
            window = candles[: i + 1]
            if strategy.check_signal(window).action == "buy":
                buy_window = window
                break
        assert buy_window is not None, "не нашли ни одного buy на осцилляции"

        price = buy_window[-1].close
        atr_value = atr(
            [c.high for c in buy_window],
            [c.low for c in buy_window],
            [c.close for c in buy_window],
            14,
        )[-1]
        signal = strategy.check_signal(buy_window)
        assert signal.stop_loss is not None and signal.take_profit is not None
        assert abs(signal.stop_loss - (price - 1.5 * atr_value)) < 1e-6
        assert abs(signal.take_profit - (price + 2.5 * atr_value)) < 1e-6

    def test_buy_forbidden_below_sma200(self) -> None:
        # Весь ряд опущен вниз: цена всегда ниже SMA200 -> покупок нет
        rows = self._sine_rows(310, amplitude=2.0, wick=0.3)
        drop = 8.0
        shifted = [(o - drop, h - drop, lo - drop, c - drop) for o, h, lo, c in rows]
        strategy = AdaptiveAtrStrategy(trend_period=200, min_atr_pct=0.05)
        candles = make_candles(shifted)

        for i in range(220, len(candles)):
            signal = strategy.check_signal(candles[: i + 1])
            assert signal.action != "buy", "покупка запрещена при цене ниже SMA200"

    def test_too_quiet_market_is_skipped(self) -> None:
        # Сегмент 2 почти плоский (центр 100, дрожание 0.001):
        # его ATR ничтожен, но SMA200 опущен сегментом 1 (центр 95),
        # поэтому золотое пересечение проходит тренд и упирается
        # во фильтр минимального ATR.
        strategy = AdaptiveAtrStrategy(trend_period=200, min_atr_pct=0.1)
        rows = self._sine_rows(150, amplitude=2.0, wick=0.001, center=95.0)
        rows += self._sine_rows(160, amplitude=0.001, wick=0.001, phase=150)
        candles = make_candles(rows)

        found_quiet_reason = False
        for i in range(230, len(candles)):
            signal = strategy.check_signal(candles[: i + 1])
            if signal.action == "hold" and "низкий" in signal.reason:
                found_quiet_reason = True
                break
        assert found_quiet_reason, "фильтр минимального ATR не заблокировал тихий рынок"


# ========================== Свинг-уровни ==========================


class TestSwingLevelsStrategy:
    def _scenario_rows(self) -> tuple[list, float]:
        """Сцена: плоскость -> горка (свинг) -> пробой.

        Returns:
            Кортеж (строки до момента сразу после пробоя, уровень).
        """
        level = 110.2
        rows: list = []
        rows += flat(100.0, 20)  # базовая плоскость
        # Горка вверх к пику 110.2
        peak_path = [101, 103, 105, 108, 110]
        for c in peak_path:
            rows.append((c - 0.2, c + 0.2, c - 0.3, c))
        # Спуск обратно (правые соседи свинга)
        down_path = [108, 106, 104, 102, 100]
        for c in down_path:
            rows.append((c + 0.2, c + 0.3, c - 0.2, c))
        rows += flat(100.0, 12)
        # Пробой: закрытие выше уровня
        rows.append((111.5, 112.3, 111.3, 112.0))
        return rows, level

    def test_levels_found_and_breakout_registered_then_retest_buys(self) -> None:
        strategy = SwingLevelsStrategy(lookback=35, wing_size=5)
        rows, level = self._scenario_rows()

        # Окно заканчивается свечой пробоя: сетап зарегистрирован, входа нет
        breakout_candles = make_candles(rows)
        signal = strategy.check_signal(breakout_candles)
        assert signal.action == "hold"
        assert level in strategy.resistance_levels
        assert strategy._long_setup is not None
        assert strategy._long_setup["level"] == level

        # Свеча ретеста: коснулась уровня и закрылась над ним бычьим телом
        retest_row = (110.0, 110.8, 109.9, 110.6)
        full_candles = make_candles(rows + [retest_row])
        signal = strategy.check_signal(full_candles)
        assert signal.action == "buy"
        assert "ретест" in signal.reason.lower()

    def test_no_entry_if_retest_candle_is_bearish(self) -> None:
        strategy = SwingLevelsStrategy(lookback=35, wing_size=5)
        rows, _level = self._scenario_rows()
        # Медвежья свеча ретеста: закрытие НИЖЕ открытия
        bearish_retest = (111.0, 111.2, 109.9, 110.1)
        signal = strategy.check_signal(make_candles(rows + [bearish_retest]))
        assert signal.action == "hold"

    def test_short_scenario_breakdown_and_retest(self) -> None:
        strategy = SwingLevelsStrategy(lookback=35, wing_size=5)
        level = 89.8
        rows: list = []
        rows += flat(100.0, 20)
        # Яма вниз к локальному минимуму
        for c in [99, 97, 95, 92, 90]:
            rows.append((c + 0.2, c + 0.3, c - 0.2, c))
        for c in [92, 94, 96, 98, 100]:
            rows.append((c - 0.2, c + 0.2, c - 0.3, c))
        rows += flat(100.0, 12)
        # Пробой поддержки вниз
        rows.append((88.5, 88.7, 87.7, 88.0))

        candles = make_candles(rows)
        assert strategy.check_signal(candles).action == "hold"
        assert level in strategy.support_levels

        # Ретест снизу: коснулась уровня сверху, закрылась под ним медвежьим
        retest_row = (90.0, 90.1, 89.4, 89.5)
        signal = strategy.check_signal(make_candles(rows + [retest_row]))
        assert signal.action == "sell"

    def test_insufficient_data_holds(self) -> None:
        strategy = SwingLevelsStrategy(lookback=50, wing_size=5)
        signal = strategy.check_signal(make_candles(flat(100.0, 40)))
        assert signal.action == "hold"
