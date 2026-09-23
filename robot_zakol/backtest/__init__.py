"""Честный бэктестер robot_zakol: tick/1s/1m + 3 fill-модели."""

from __future__ import annotations

from robot_zakol.backtest.backtest import run_modes, run_single
from robot_zakol.backtest.config import FillConfig, StrategyConfig, mode_presets

__all__ = ["FillConfig", "StrategyConfig", "mode_presets", "run_modes", "run_single"]
