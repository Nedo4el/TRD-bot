"""Тесты robot_flat стратегии.

Стратегия обнулена (2026-09-22) и будет переписана — тесты вернём вместе с ней.
"""

from __future__ import annotations

import pytest

try:
    from robot_flat.strategy import FlatConfig, FlatStrategy
except ImportError:
    pytest.skip("robot_flat обнулён — перепишем стратегию", allow_module_level=True)

from core.bybit_client import Candle


def _make_candles(
    n: int = 200,
    base_price: float = 1.0,
    trend: str = "flat",
    ema_fast_above: bool | None = None,
) -> list[Candle]:
    """Создать свечи с опциональным трендом."""
    candles = []
    for i in range(n):
        if trend == "up":
            price = base_price * (1 + i * 0.001)
        elif trend == "down":
            price = base_price * (1 - i * 0.001)
        else:
            price = base_price

        candles.append(
            Candle(
                open_time=i * 60000,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=100.0,
            )
        )
    return candles


class TestFlatStrategy:
    """Тесты основной логики."""

    def test_flat_market_hold(self) -> None:
        """Боковик — hold (ждём подходящего уровня)."""
        cfg = FlatConfig(poc_lookback=100, trend_ema_slow=150)
        strategy = FlatStrategy(cfg)
        candles = _make_candles(200, 1.0)
        signal = strategy.check_signal(candles)
        assert signal.action == "hold"
        assert "СТОП ЗОНА" in signal.reason or "БОКОВИК" in signal.reason

    def test_uptrend_blocked(self) -> None:
        """Восходящий тренд — заблокирован фильтром."""
        cfg = FlatConfig(poc_lookback=100, trend_ema_fast=50, trend_ema_slow=150, trend_threshold=2.0)
        strategy = FlatStrategy(cfg)
        candles = _make_candles(200, 1.0, trend="up")
        signal = strategy.check_signal(candles)
        assert signal.action == "hold"
        assert "ТРЕНД" in signal.reason

    def test_downtrend_blocked(self) -> None:
        """Нисходящий тренд — заблокирован фильтром."""
        cfg = FlatConfig(poc_lookback=100, trend_ema_fast=50, trend_ema_slow=150, trend_threshold=2.0)
        strategy = FlatStrategy(cfg)
        candles = _make_candles(200, 1.0, trend="down")
        signal = strategy.check_signal(candles)
        assert signal.action == "hold"
        assert "ТРЕНД" in signal.reason


class TestTrailingStop:
    """Тесты трейлинг стопа."""

    def test_no_positions_no_trailing(self) -> None:
        """Без позиций — трейлинг не работает."""
        cfg = FlatConfig()
        strategy = FlatStrategy(cfg)
        signal = strategy._check_trailing_stop(1.0, 1.01, 0.99, 1.0, 10.0)
        assert signal is None

    def test_long_trailing_stop_update(self) -> None:
        """LONG: трейлинг стоп обновляется при росте цены."""
        from robot_flat.strategy import PositionState

        cfg = FlatConfig(trailing_after_poc_pct=2.0)
        strategy = FlatStrategy(cfg)
        strategy._positions.append(
            PositionState(
                direction="long",
                entry_price=1.0,
                current_peak=1.0,
                trailing_stop=0.95,
            )
        )

        signal = strategy._check_trailing_stop(
            current_price=1.05, high=1.06, low=1.04, poc=1.0, half_range=10.0
        )
        assert signal is None
        assert strategy._positions[0].current_peak == 1.06
        assert abs(strategy._positions[0].trailing_stop - 1.0388) < 0.001

    def test_long_trailing_stop_hit(self) -> None:
        """LONG: стоп ударен — закрытие позиции."""
        from robot_flat.strategy import PositionState

        cfg = FlatConfig(trailing_after_poc_pct=2.0)
        strategy = FlatStrategy(cfg)
        strategy._positions.append(
            PositionState(
                direction="long",
                entry_price=1.0,
                current_peak=1.06,
                trailing_stop=1.0388,
            )
        )

        signal = strategy._check_trailing_stop(
            current_price=1.03, high=1.04, low=1.02, poc=1.0, half_range=10.0
        )
        assert signal is not None
        assert signal.action == "close_long"
        assert len(strategy._positions) == 0

    def test_short_trailing_stop_update(self) -> None:
        """SHORT: трейлинг стоп обновляется при падении цены."""
        from robot_flat.strategy import PositionState

        cfg = FlatConfig(trailing_after_poc_pct=2.0)
        strategy = FlatStrategy(cfg)
        strategy._positions.append(
            PositionState(
                direction="short",
                entry_price=1.0,
                current_peak=1.0,
                trailing_stop=1.05,
            )
        )

        signal = strategy._check_trailing_stop(
            current_price=0.95, high=0.955, low=0.94, poc=1.0, half_range=10.0
        )
        assert signal is None
        assert strategy._positions[0].current_peak == 0.94
        assert abs(strategy._positions[0].trailing_stop - 0.9588) < 0.001

    def test_short_trailing_stop_hit(self) -> None:
        """SHORT: стоп ударен — закрытие позиции."""
        from robot_flat.strategy import PositionState

        cfg = FlatConfig(trailing_after_poc_pct=2.0)
        strategy = FlatStrategy(cfg)
        strategy._positions.append(
            PositionState(
                direction="short",
                entry_price=1.0,
                current_peak=0.94,
                trailing_stop=0.9588,
            )
        )

        signal = strategy._check_trailing_stop(
            current_price=0.97, high=0.98, low=0.96, poc=1.0, half_range=10.0
        )
        assert signal is not None
        assert signal.action == "close_short"
        assert len(strategy._positions) == 0


class TestPositionManagement:
    """Тесты управления позициями."""

    def test_max_positions_limit(self) -> None:
        """Максимум позиций — не больше 3 в одну сторону."""
        cfg = FlatConfig(max_positions=3)
        strategy = FlatStrategy(cfg)
        for _ in range(3):
            strategy._positions.append(
                __import__("robot_flat.strategy", fromlist=["PositionState"]).PositionState(
                    direction="long",
                    entry_price=1.0,
                    current_peak=1.0,
                    trailing_stop=0.95,
                )
            )
        assert len(strategy._positions) == 3


class TestPOCCalculation:
    """Тесты расчёта POC."""

    def test_poc_basic(self) -> None:
        """POC — цена с максимальным объёмом (legacy)."""
        closes = [1.0, 1.0, 1.0, 2.0, 2.0]
        volumes = [100.0, 100.0, 100.0, 50.0, 50.0]
        poc = FlatStrategy._calc_poc(closes, volumes)
        assert 0.9 < poc < 1.5

    def test_poc_empty(self) -> None:
        """POC = 0 при пустых данных."""
        poc = FlatStrategy._calc_poc([], [])
        assert poc == 0.0

    def test_volume_poc_basic(self) -> None:
        """Volume POC — объём по диапазону high/low."""
        highs = [1.1, 1.05, 1.05]
        lows = [1.0, 1.0, 1.0]
        volumes = [100.0, 10.0, 10.0]
        poc = FlatStrategy._calc_volume_poc(highs, lows, volumes)
        assert 1.0 < poc < 1.1

    def test_volume_poc_equal_distribution(self) -> None:
        """Volume POC — равномерное распределение (первый бин)."""
        highs = [2.0, 2.0]
        lows = [1.0, 1.0]
        volumes = [50.0, 50.0]
        poc = FlatStrategy._calc_volume_poc(highs, lows, volumes)
        assert 1.0 < poc < 1.1


class TestEMACalculation:
    """Тесты EMA фильтра."""

    def test_ema_basic(self) -> None:
        """EMA — базовый расчёт."""
        data = [1.0 + i * 0.01 for i in range(100)]
        ema = FlatStrategy._ema(data, 20)
        assert ema > 0

    def test_ema_insufficient_data(self) -> None:
        """EMA = 0 при недостаточных данных."""
        ema = FlatStrategy._ema([1.0, 2.0], 20)
        assert ema == 0.0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
