from __future__ import annotations

import pytest

from backtest import run_backtest
from core.bybit_client import Candle
from core.strategies import Signal
from robot_trend.risk import cooldown_remaining, on_price
from robot_trend.strategy import TrendParams, TrendStrategy, _Snapshot


@pytest.mark.parametrize(
    ("side", "price", "initial_stop", "expected_stop", "active"),
    [
        ("long", 107.99, 98.0, 98.0, False),
        ("long", 108.0, 98.0, 103.68, True),
        ("long", 120.0, 98.0, 115.2, True),
        ("short", 92.01, 102.0, 102.0, False),
        ("short", 92.0, 102.0, 95.68, True),
        ("short", 80.0, 102.0, 83.2, True),
    ],
)
def test_trailing_activates_at_eight_percent(
    side: str,
    price: float,
    initial_stop: float,
    expected_stop: float,
    active: bool,
) -> None:
    decision = on_price(
        entry=100.0,
        peak=price,
        price=price,
        current_stop=initial_stop,
        stop_pct=0.02,
        be_trigger_pct=0.08,
        be_offset_pct=0.0,
        trail_pct=0.04,
        side=side,
    )
    assert decision.stop_loss == pytest.approx(expected_stop)
    assert decision.be_active is active


@pytest.mark.parametrize("side", ["long", "short"])
def test_backtest_zero_take_allows_trailing_exit(side: str) -> None:
    params = TrendParams(take_pct=0, be_trigger_pct=0.08, trail_pct=0.04)

    class EntryOnce(TrendStrategy):
        _min_warmup = 0

        def check_signal(self, candles: list[Candle]) -> Signal:
            if len(candles) != 1:
                return Signal(action="hold", reason="")
            snapshot = _Snapshot(100, 100, 99, 1, 2, True, True)
            stop, take = self._stop_take(snapshot, side)
            return Signal(
                action="buy" if side == "long" else "sell",
                reason="",
                stop_loss=stop,
                take_profit=take,
            )

    prices = (
        [(101, 99, 100), (109, 100, 108), (120, 106, 119), (119, 114, 115)]
        if side == "long"
        else [(101, 99, 100), (100, 91, 92), (94, 80, 81), (84, 81, 83)]
    )
    candles = [
        Candle(i * 300_000, close, high, low, close, 100)
        for i, (high, low, close) in enumerate([*prices, prices[-1]])
    ]

    result = run_backtest(candles, EntryOnce(params), 2, 8)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_time == 900_000
    assert trade.exit_price == pytest.approx(115.2 if side == "long" else 83.2)


@pytest.mark.parametrize(("elapsed", "remaining"), [(299, 1), (300, 0)])
def test_cooldown_releases_entry_after_five_minutes(
    elapsed: int, remaining: int
) -> None:
    assert cooldown_remaining(1000, 1000 + elapsed, 300) == remaining
