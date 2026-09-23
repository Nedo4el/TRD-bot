"""Параметры стратегии и fill-модели (все допущения — здесь)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    """Параметры «Плавающие лимитки с коротким TTL»."""

    offset_pct: float = 0.03
    ttl_sec: float = 20.0
    min_price_change: float = 0.003
    stop_pct: float = 0.02
    trail_pct: float = 0.02
    be_trigger_pct: float = 0.01
    max_loss_usd: float = 10.0
    max_consecutive_losses: int = 5
    deposit_usd: float = 100.0
    position_pct: float = 10.0
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


def mode_presets(
    deposit: float = 100.0,
) -> dict[str, tuple[StrategyConfig, FillConfig]]:
    """Три режима сравнения: ideal / realistic / pessimistic."""
    strat = StrategyConfig(deposit_usd=deposit)
    return {
        "ideal": (
            strat,
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
            strat,
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
            strat,
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
