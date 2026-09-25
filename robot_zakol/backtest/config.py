"""Параметры стратегии и fill-модели (все допущения — здесь)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from robot_zakol.config import ZakolConfig


@dataclass(frozen=True)
class StrategyConfig:
    """Параметры «Плавающие лимитки ±offset с коротким TTL»."""

    offset_pct: float = 0.03
    ttl_sec: float = 20.0
    min_price_change: float = 0.003
    stop_pct: float = 0.02
    take_pct: float = 0.05
    trail_pct: float = 0.02
    be_trigger_pct: float = 0.005
    be_offset_pct: float = 0.002
    max_loss_usd: float = 10.0
    max_drawdown_pct: float = 0.05
    deposit_usd: float = 100.0
    position_pct: float = 10.0
    partial_fill_pct: float = 0.80
    tick_size: float = 0.0001


@dataclass(frozen=True)
class FillConfig:
    """Допущения fill-модели — все числа явные."""

    name: str = "realistic"
    ideal: bool = False
    queue_factor: float = 1.0
    latency_ms: float = 100.0
    slippage_pct: float = 0.0005
    post_only_strict: bool = True
    allow_partial: bool = True
    seed: int = 42


def strategy_from_zakol(z: ZakolConfig) -> StrategyConfig:
    """Собрать параметры бэктеста из .env robot_zakol (единый источник)."""
    return StrategyConfig(
        offset_pct=z.offset_pct,
        ttl_sec=z.ttl_sec,
        min_price_change=z.min_price_change,
        stop_pct=z.stop_pct,
        take_pct=z.take_pct,
        trail_pct=z.trail_pct,
        be_trigger_pct=z.be_trigger_pct,
        be_offset_pct=z.be_offset_pct,
        max_loss_usd=z.max_loss_usd,
        max_drawdown_pct=z.max_drawdown_pct,
        deposit_usd=z.deposit_usd,
        position_pct=z.position_pct,
        partial_fill_pct=z.partial_fill_pct,
    )


def mode_presets(
    deposit: float = 100.0,
    strat: StrategyConfig | None = None,
) -> dict[str, tuple[StrategyConfig, FillConfig]]:
    """Три режима сравнения: ideal / realistic / pessimistic."""
    base = (
        replace(strat, deposit_usd=deposit)
        if strat
        else StrategyConfig(deposit_usd=deposit)
    )
    return {
        "ideal": (
            base,
            FillConfig(
                name="ideal",
                ideal=True,
                queue_factor=0.0,
                latency_ms=0.0,
                slippage_pct=0.0,
                post_only_strict=False,
                allow_partial=False,
            ),
        ),
        "realistic": (
            base,
            FillConfig(
                name="realistic",
                ideal=False,
                queue_factor=1.0,
                latency_ms=100.0,
                slippage_pct=0.0005,
                post_only_strict=True,
                allow_partial=True,
            ),
        ),
        "pessimistic": (
            base,
            FillConfig(
                name="pessimistic",
                ideal=False,
                queue_factor=2.0,
                latency_ms=200.0,
                slippage_pct=0.001,
                post_only_strict=True,
                allow_partial=True,
            ),
        ),
    }
