"""Тесты риск-менеджера: размер позиции, стопы, тейки."""

from __future__ import annotations

from core.config import Config
from core.risk_manager import RiskManager


def make_config() -> Config:
    """Конфиг с фиксированными значениями (без чтения .env)."""
    return Config(
        api_key="test",
        api_secret="test",
        symbol="XRPUSDT",
        position_pct=10.0,
        fixed_qty=0.0,
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
    )


class TestPositionSize:
    def test_percent_of_balance(self) -> None:
        config = make_config()
        risk = RiskManager(config)
        # 10% от 1000 USDT по цене 100 -> 1.0 актива
        assert risk.calculate_position_size(1000.0, 100.0) == 1.0

    def test_fixed_qty_overrides_percent(self) -> None:
        config = make_config()
        config.fixed_qty = 0.01
        risk = RiskManager(config)
        # Фиксированный лот не зависит ни от баланса, ни от цены
        assert risk.calculate_position_size(999999.0, 0.5) == 0.01

    def test_zero_balance_raises(self) -> None:
        import pytest

        risk = RiskManager(make_config())
        with pytest.raises(ValueError):
            risk.calculate_position_size(0.0, 100.0)


class TestStops:
    def test_long_stops(self) -> None:
        risk = RiskManager(make_config())
        sl = risk.calculate_stop_loss(100.0, "Buy")
        tp = risk.calculate_take_profit(100.0, "Buy")
        assert sl == 98.0  # -2%
        assert tp == 104.0  # +4%

    def test_short_stops_mirrored(self) -> None:
        risk = RiskManager(make_config())
        sl = risk.calculate_stop_loss(100.0, "Sell")
        tp = risk.calculate_take_profit(100.0, "Sell")
        assert sl == 102.0
        assert tp == 96.0

    def test_build_plan_uses_fixed_qty(self) -> None:
        config = make_config()
        config.fixed_qty = 0.05
        plan = RiskManager(config).build_plan(1000.0, 50.0, "Buy")
        assert plan.qty == 0.05
        assert plan.entry_price == 50.0
        assert plan.stop_loss < plan.take_profit
