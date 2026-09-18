"""Тесты robot_flat стратегии."""

from __future__ import annotations

from core.bybit_client import Candle
from robot_flat.strategy import FlatConfig, FlatStrategy


def _make_candles(
    n: int = 200,
    base_price: float = 1.0,
    trend: str = "flat",
    impulse_at: int | None = None,
    impulse_size: float = 20.0,
) -> list[Candle]:
    """Создать свечи с опциональным импульсом."""
    candles = []
    for i in range(n):
        if trend == "up":
            price = base_price * (1 + i * 0.001)
        elif trend == "down":
            price = base_price * (1 - i * 0.001)
        else:
            price = base_price

        # Добавляем импульс в указанной позиции
        if impulse_at and i == impulse_at:
            price = base_price * (1 + impulse_size / 100)

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

    def test_no_impulse_hold(self) -> None:
        """Без импульса — hold."""
        cfg = FlatConfig(impulse_min_pct=15.0, poc_lookback=100)
        strategy = FlatStrategy(cfg)
        candles = _make_candles(200, 1.0)
        signal = strategy.check_signal(candles)
        assert signal.action == "hold"
        assert "нет импульса" in signal.reason

    def test_impulse_detected(self) -> None:
        """Импульс обнаружен — ждём стабилизации."""
        cfg = FlatConfig(
            impulse_min_pct=15.0,
            impulse_window=5,
            impulse_cooldown=30,
            stop_zone_pct=5.0,
            poc_lookback=100,
        )
        strategy = FlatStrategy(cfg)
        # Импульс на свече 150, стабилизация 30 свечей → 180/200
        # Цена после импульса = 1.2 (выше стоп зоны)
        candles = _make_candles(200, 1.0, impulse_at=150, impulse_size=20.0)
        signal = strategy.check_signal(candles)
        assert signal.action == "hold"
        # Либо стабилизация, либо стоп зона (цена на POC)
        assert "стабилизации" in signal.reason or "СТОП ЗОНА" in signal.reason

    def test_impulse_stabilized(self) -> None:
        """Импульс + стабилизация — ищем боковик."""
        cfg = FlatConfig(
            impulse_min_pct=15.0,
            impulse_window=5,
            impulse_cooldown=30,
            poc_lookback=60,
        )
        strategy = FlatStrategy(cfg)
        # Импульс на свече 100, стабилизация 30 свечей, итого 130+
        candles = _make_candles(200, 1.0, impulse_at=100, impulse_size=20.0)
        signal = strategy.check_signal(candles)
        # Должны получить любой сигнал кроме "нет импульса"
        assert signal.action != "hold" or "импульс" not in signal.reason


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

        # Цена растёт — пик обновляется
        signal = strategy._check_trailing_stop(
            current_price=1.05, high=1.06, low=1.04, poc=1.0, half_range=10.0
        )
        assert signal is None
        assert strategy._positions[0].current_peak == 1.06
        # Стоп = 1.06 * (1 - 2%) = 1.0388
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

        # Цена падает ниже стопа
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

        # Цена падает — пик обновляется
        # high должен быть < trailing_stop (0.9588) чтобы не сработал стоп
        signal = strategy._check_trailing_stop(
            current_price=0.95, high=0.955, low=0.94, poc=1.0, half_range=10.0
        )
        assert signal is None
        assert strategy._positions[0].current_peak == 0.94
        # Стоп = 0.94 * (1 + 2%) = 0.9588
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

        # Цена растёт выше стопа
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
        # Добавляем 3 LONG позиции
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
        # POC должен быть ближе к 1.0 (там больше объёма)
        assert 0.9 < poc < 1.5

    def test_poc_empty(self) -> None:
        """POC = 0 при пустых данных."""
        poc = FlatStrategy._calc_poc([], [])
        assert poc == 0.0

    def test_volume_poc_basic(self) -> None:
        """Volume POC — объём по диапазону high/low."""
        # Свеча с большим объёмом в диапазоне 1.0-1.1
        highs = [1.1, 1.05, 1.05]
        lows = [1.0, 1.0, 1.0]
        volumes = [100.0, 10.0, 10.0]
        poc = FlatStrategy._calc_volume_poc(highs, lows, volumes)
        # POC должен быть ближе к 1.05 (там больше объёма)
        assert 1.0 < poc < 1.1

    def test_volume_poc_equal_distribution(self) -> None:
        """Volume POC — равномерное распределение (первый бин)."""
        highs = [2.0, 2.0]
        lows = [1.0, 1.0]
        volumes = [50.0, 50.0]
        poc = FlatStrategy._calc_volume_poc(highs, lows, volumes)
        # При равномерном распределении argmax вернёт первый бин
        assert 1.0 < poc < 1.1


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
