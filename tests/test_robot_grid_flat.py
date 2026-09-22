"""Тесты robot_grid_flat: чистые функции сетки и стейт-машина."""

from __future__ import annotations

import pytest

from core.bybit_client import Candle
from robot_grid_flat.strategy import (
    GridConfig,
    GridStrategy,
    InstrumentInfo,
    MarketSnapshot,
    ceil_to_tick,
    compute_step,
    floor_to_tick,
    level_key,
    level_qty,
    make_levels,
    size_levels,
    tp_price_for,
    volume_profile,
)

INSTRUMENT = InstrumentInfo(
    tick_size=0.001,
    qty_step=0.1,
    min_qty=1.0,
    min_notional=5.0,
)

TF_MS = 5 * 60 * 1000
BASE_TIME = 1_700_000_000_000


def flat_candles(n: int = 60, base: float = 1.0, start: int = 0) -> list[Candle]:
    """Плоские свечи вокруг base (стабильный POC)."""
    return [
        Candle(
            open_time=BASE_TIME + (start + i) * TF_MS,
            open=base,
            high=base * 1.01,
            low=base * 0.99,
            close=base,
            volume=100.0,
        )
        for i in range(n)
    ]


def falling_candles(n: int = 60, base: float = 1.0, drop: float = 0.02) -> list[Candle]:
    """Нисходящий тренд drop на свечу (для EMA-тестов)."""
    candles: list[Candle] = []
    price = base
    for i in range(n):
        nxt = price * (1 - drop)
        candles.append(
            Candle(
                open_time=BASE_TIME + i * TF_MS,
                open=price,
                high=price * 1.005,
                low=nxt * 0.995,
                close=nxt,
                volume=100.0,
            )
        )
        price = nxt
    return candles


def impulse_candles(n: int = 60, base: float = 1.0, jump: float = 0.06) -> list[Candle]:
    """Плоские свечи с импульсом `jump` на последнем баре."""
    candles = flat_candles(n - 1, base)
    last = base * (1 + jump)
    candles.append(
        Candle(
            open_time=BASE_TIME + (n - 1) * TF_MS,
            open=base,
            high=last * 1.005,
            low=base * 0.995,
            close=last,
            volume=100.0,
        )
    )
    return candles


def make_snap(
    candles: list[Candle] | None = None,
    *,
    price: float = 1.0,
    equity: float = 1000.0,
    now: float = 1_700_000_000.0,
    long_qty: float = 0.0,
    short_qty: float = 0.0,
    funding_pct: float | None = None,
    spread_pct: float | None = 0.01,
    depth_notional: float | None = None,
    news_ok: bool = True,
    manual_pause: bool = False,
) -> MarketSnapshot:
    """Собрать MarketSnapshot с разумными дефолтами для тестов."""
    return MarketSnapshot(
        now=now,
        candles=candles if candles is not None else flat_candles(),
        price=price,
        bid=price * 0.9999,
        ask=price * 1.0001,
        equity=equity,
        long_qty=long_qty,
        short_qty=short_qty,
        funding_pct=funding_pct,
        spread_pct=spread_pct,
        depth_notional=depth_notional,
        news_ok=news_ok,
        manual_pause=manual_pause,
        tf_minutes=5,
        instrument=INSTRUMENT,
    )


def make_cfg(**overrides: object) -> GridConfig:
    """Конфиг с быстрым окном профиля для тестов."""
    base: dict = {"poc_window_min": 60}
    base.update(overrides)
    return GridConfig(**base)


def build_strategy(**cfg_overrides: object) -> tuple[GridStrategy, MarketSnapshot]:
    """Стратегия + первый снапшот, на котором строится сетка."""
    strategy = GridStrategy(make_cfg(**cfg_overrides))
    snap = make_snap()
    decision = strategy.next(snap)
    assert decision.kind == "build", decision.reason
    return strategy, snap


# ========================= Чистые функции ================================


class TestVolumeProfile:
    def test_poc_in_heavy_region(self) -> None:
        """POC там, где сконцентрирован объём."""
        highs = [1.1, 1.1, 2.1]
        lows = [1.0, 1.0, 2.0]
        volumes = [100.0, 100.0, 10.0]
        profile = volume_profile(highs, lows, volumes, n_bins=100)
        assert profile is not None
        assert 1.0 <= profile.poc <= 1.1
        assert profile.val <= profile.poc <= profile.vah
        assert profile.total_volume == 210.0

    def test_value_area_contains_most_volume(self) -> None:
        """VA (70%) включает POC и уже всего диапазона."""
        highs = [1.05] * 20 + [1.5] * 20
        lows = [1.0] * 20 + [1.45] * 20
        volumes = [100.0] * 20 + [5.0] * 20
        profile = volume_profile(highs, lows, volumes, n_bins=100)
        assert profile is not None
        assert profile.val <= profile.poc <= profile.vah
        assert (profile.vah - profile.val) < 0.5

    def test_empty_inputs(self) -> None:
        """Пустые/разные входы → None."""
        assert volume_profile([], [], []) is None
        assert volume_profile([1.0], [], [1.0]) is None
        assert volume_profile([1.0], [0.9], [0.0]) is None

    def test_flat_range(self) -> None:
        """Диапазон нулевой ширины → POC = VAH = VAL."""
        profile = volume_profile([1.0, 1.0], [1.0, 1.0], [5.0, 5.0])
        assert profile is not None
        assert profile.poc == profile.vah == profile.val == 1.0


class TestComputeStep:
    def test_atr_dominates(self) -> None:
        """0.3×ATR больше тиков и комиссии."""
        cfg = make_cfg()
        step = compute_step(atr_val=1.0, price=100.0, tick=0.01, cfg=cfg)
        assert step >= 0.3 * 1.0
        assert step / 0.01 == pytest.approx(round(step / 0.01))

    def test_tick_floor(self) -> None:
        """3×tick доминирует, когда ATR крошечный."""
        cfg = make_cfg()
        step = compute_step(atr_val=0.0, price=1.0, tick=0.01, cfg=cfg)
        assert step >= 0.03

    def test_fee_floor(self) -> None:
        """Шаг окупает 3×taker-комиссию от цены."""
        cfg = make_cfg()
        step = compute_step(atr_val=0.0, price=100.0, tick=0.001, cfg=cfg)
        assert step >= 3 * cfg.fee_taker * 100.0

    def test_ceil_to_tick(self) -> None:
        assert ceil_to_tick(0.1001, 0.01) == 0.11
        assert floor_to_tick(0.1099, 0.01) == 0.10
        assert ceil_to_tick(0.1, 0.01) == 0.1


class TestMakeLevels:
    def test_no_level_on_poc_and_inside_bounds(self) -> None:
        levels = make_levels(poc=1.0, lower=0.9, upper=1.1, step=0.02, tick=0.001)
        assert levels
        assert all(abs(p - 1.0) > 0.001 / 2 for p in levels)
        assert all(0.9 - 0.0005 <= p <= 1.1 + 0.0005 for p in levels)

    def test_sorted_by_distance_to_poc(self) -> None:
        levels = make_levels(poc=1.0, lower=0.9, upper=1.1, step=0.02, tick=0.001)
        distances = [abs(p - 1.0) for p in levels]
        assert distances == sorted(distances)

    def test_invalid_bounds(self) -> None:
        assert make_levels(1.0, 1.1, 0.9, 0.01, 0.001) == []
        assert make_levels(1.0, 0.9, 1.1, 0.0, 0.001) == []


class TestSizeLevels:
    def test_basic(self) -> None:
        cfg = make_cfg()
        per = size_levels(
            equity=1000.0,
            poc=1.0,
            lower=0.95,
            upper=1.05,
            atr_val=0.02,
            n_levels=10,
            cfg=cfg,
        )
        assert per is not None and per > 0

    def test_exposure_cap(self) -> None:
        cfg = make_cfg(risk_per_trade_pct=50.0, max_exposure_pct=20.0)
        per = size_levels(
            equity=1000.0,
            poc=1.0,
            lower=0.95,
            upper=1.05,
            atr_val=0.02,
            n_levels=10,
            cfg=cfg,
        )
        assert per is not None
        assert per * 10 <= 1000.0 * 0.20 + 1e-9

    def test_order_cap(self) -> None:
        cfg = make_cfg(order_size_cap_usd=7.0, risk_per_trade_pct=50.0)
        per = size_levels(
            equity=1000.0,
            poc=1.0,
            lower=0.95,
            upper=1.05,
            atr_val=0.02,
            n_levels=10,
            cfg=cfg,
        )
        assert per == 7.0

    def test_zero_equity(self) -> None:
        cfg = make_cfg()
        assert size_levels(0.0, 1.0, 0.9, 1.1, 0.02, 10, cfg) is None


class TestQtyAndTP:
    def test_level_qty_floors_to_step(self) -> None:
        assert level_qty(19.99, 1.0, INSTRUMENT) == pytest.approx(19.9)
        assert level_qty(0.05, 1.0, INSTRUMENT) == 0.0

    def test_tp_buy_above_entry(self) -> None:
        tp = tp_price_for("Buy", entry_price=1.0, step=0.02, fee_maker=0.0002)
        assert 1.0 < tp < 1.02
        gross = tp - 1.0
        fees = 1.0 * 0.0002 * 2
        assert gross > fees

    def test_tp_sell_below_entry(self) -> None:
        tp = tp_price_for("Sell", entry_price=1.0, step=0.02, fee_maker=0.0002)
        assert 0.98 < tp < 1.0

    def test_level_key(self) -> None:
        assert level_key("Buy", 1.5) != level_key("Sell", 1.5)


# ========================= Стейт-машина ===================================


class TestStateMachine:
    def test_warmup_wait(self) -> None:
        """Мало свечей — wait без сетки."""
        strategy = GridStrategy(make_cfg())
        snap = make_snap(candles=flat_candles(5))
        decision = strategy.next(snap)
        assert decision.kind == "wait"
        assert "разогрев" in decision.reason
        assert strategy.plan is None

    def test_build_places_entries(self) -> None:
        """Достаточно данных → build с входами с двух сторон."""
        strategy = GridStrategy(make_cfg())
        decision = strategy.next(make_snap())
        assert decision.kind == "build"
        assert decision.plan is not None
        sides = {e.side for e in decision.entries}
        assert sides == {"Buy", "Sell"}
        assert all(e.usd > 0 for e in decision.entries)

    def test_stable_run_after_build(self) -> None:
        """Без триггеров — обычный run."""
        strategy, _ = build_strategy()
        snap = make_snap(candles=flat_candles(60, start=60))
        decision = strategy.next(snap)
        assert decision.kind == "run"

    def test_manual_pause_after_build(self) -> None:
        """Файл PAUSE: пауза входов, сетка остаётся."""
        strategy, _ = build_strategy()
        decision = strategy.next(make_snap(manual_pause=True))
        assert decision.kind == "pause"
        assert decision.cancel_entries
        assert "ПАУЗА" in decision.reason
        assert strategy.plan is not None

    def test_news_guard(self) -> None:
        """Окно новостей блокирует входы."""
        strategy = GridStrategy(make_cfg())
        decision = strategy.next(make_snap(news_ok=False))
        assert decision.kind == "wait"
        assert "НОВОСТИ" in decision.reason

    def test_funding_guard(self) -> None:
        """|funding| > 0.05% — пауза."""
        strategy = GridStrategy(make_cfg())
        decision = strategy.next(make_snap(funding_pct=0.1))
        assert decision.kind == "wait"
        assert "FUNDING" in decision.reason

    def test_spread_guard(self) -> None:
        """Спред > 0.05% — пауза."""
        strategy = GridStrategy(make_cfg())
        decision = strategy.next(make_snap(spread_pct=0.2))
        assert decision.kind == "wait"
        assert "СПРЕД" in decision.reason

    def test_impulse_guard(self) -> None:
        """Импульс 6% за 5 баров — кулдаун входов."""
        strategy = GridStrategy(make_cfg())
        decision = strategy.next(make_snap(candles=impulse_candles()))
        assert decision.kind == "wait"
        assert "ИМПУЛЬС" in decision.reason

    def test_depth_guard_after_build(self) -> None:
        """Глубина стакана < 5×уровня — пауза входов."""
        strategy, _ = build_strategy()
        decision = strategy.next(make_snap(depth_notional=1.0))
        assert decision.kind == "pause"
        assert "ЛИКВИДНОСТЬ" in decision.reason

    def test_rebuild_on_price_out_of_va(self) -> None:
        """Цена вышла за VA → rebuild с отменой входов."""
        strategy, _ = build_strategy(poc_unstable_pct=100.0)
        plan = strategy.plan
        assert plan is not None
        price = (plan.vah + plan.upper) / 2
        decision = strategy.next(make_snap(price=price))
        assert decision.kind == "rebuild"
        assert decision.cancel_entries
        assert decision.plan is not None

    def test_grid_sl_resets(self) -> None:
        """Пробой границы + 1×ATR → полный сброс."""
        strategy, _ = build_strategy()
        decision = strategy.next(make_snap(price=0.9))
        assert decision.kind == "reset"
        assert decision.close_all
        assert "SL СЕТКИ" in decision.reason
        assert strategy.plan is None

    def test_kill_switch_halts(self) -> None:
        """−6% эквити за сессию → halt, бот останавливается."""
        strategy = GridStrategy(make_cfg())
        strategy.next(make_snap(equity=1000.0))
        decision = strategy.next(make_snap(equity=940.0, now=1_700_000_100.0))
        assert decision.kind == "halt"
        assert decision.close_all
        assert strategy.halted
        again = strategy.next(make_snap(equity=940.0, now=1_700_000_200.0))
        assert again.kind == "halt"

    def test_trailing_resets_after_giveback(self) -> None:
        """+4% взводит трейлинг, откат 1.5% с пика → reset."""
        strategy = GridStrategy(make_cfg())
        strategy.next(make_snap(equity=1000.0, now=1_700_000_000.0))
        strategy.next(make_snap(equity=1040.0, now=1_700_000_060.0))
        decision = strategy.next(make_snap(equity=1025.0, now=1_700_000_120.0))
        assert decision.kind == "reset"
        assert "ТРЕЙЛИНГ" in decision.reason
        assert strategy.plan is None

    def test_full_side_cancels_entries(self) -> None:
        """MAX_OPEN_LEVELS позиций на сторону — входы стороны снимаются."""
        strategy, _ = build_strategy(max_open_levels=2)
        for i, price in enumerate((0.99, 0.98)):
            strategy.register_entry("Buy", price, 20.0, f"o{i}")
            strategy.mark_entry_filled(level_key("Buy", price), price, 10.0)
        decision = strategy.next(
            make_snap(long_qty=20.0, candles=flat_candles(60, start=60))
        )
        assert decision.kind == "run"
        assert "Buy" in decision.cancel_sides
        assert all(e.side == "Sell" for e in decision.entries)

    def test_trend_blocks_side(self) -> None:
        """EMA вниз > порога при лонгах — доборные Buy блокируются."""
        strategy = GridStrategy(
            make_cfg(trend_ema_fast=5, trend_ema_slow=10, trend_threshold_pct=2.0)
        )
        strategy.next(make_snap(candles=falling_candles()))
        strategy.register_entry("Buy", 0.99, 20.0, "o1")
        strategy.mark_entry_filled(level_key("Buy", 0.99), 0.99, 10.0)
        blocked = strategy._blocked_sides(make_snap(candles=falling_candles()))
        assert "Buy" in blocked

    def test_repair_clears_positions_closed_on_exchange(self) -> None:
        """Биржа закрыла всё — локальные уровни стираются."""
        strategy, _ = build_strategy()
        key = level_key("Buy", 0.99)
        strategy.register_entry("Buy", 0.99, 20.0, "o1")
        strategy.mark_entry_filled(key, 0.99, 10.0)
        assert key in strategy.levels
        strategy.next(make_snap(long_qty=0.0))
        assert key not in strategy.levels

    def test_poc_unstable_gate(self) -> None:
        """Сдвиг POC > порога за окно → нестабилен."""
        strategy = GridStrategy(make_cfg(poc_unstable_bars=3))
        strategy._poc_history.extend([100.0, 100.1, 100.2, 106.0])
        assert strategy._poc_unstable()
        strategy._poc_history.clear()
        strategy._poc_history.extend([100.0, 100.1, 100.2, 100.3])
        assert not strategy._poc_unstable()


class TestSerialization:
    def test_roundtrip(self) -> None:
        """to_dict → restore сохраняет план, уровни и кулдауны."""
        strategy, _ = build_strategy()
        strategy.register_entry("Buy", 0.99, 20.0, "o1")
        strategy.register_sl("Buy", "sl1", 10.0, 0.95)
        data = strategy.to_dict()

        restored = GridStrategy(make_cfg())
        restored.restore(data)
        assert restored.plan is not None
        assert strategy.plan is not None
        assert restored.plan.poc == strategy.plan.poc
        assert restored.plan.step == strategy.plan.step
        assert set(restored.levels) == set(strategy.levels)
        assert restored.sl_info("Buy") == ("sl1", 10.0, 0.95)
        assert not restored.halted

    def test_halt_flag_survives(self) -> None:
        """halted=true переживает рестарт (требует ручного сброса)."""
        strategy = GridStrategy(make_cfg())
        strategy.halted = True
        strategy.halt_reason = "тест"
        restored = GridStrategy(make_cfg())
        restored.restore(strategy.to_dict())
        assert restored.halted
        assert restored.halt_reason == "тест"

    def test_unknown_version_ignored(self) -> None:
        """Чужая версия состояния не роняет бота."""
        strategy = GridStrategy(make_cfg())
        strategy.restore({"version": 999, "plan": {"poc": 1.0}})
        assert strategy.plan is None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
