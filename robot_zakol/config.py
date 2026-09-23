"""Конфигурация robot_zakol (pydantic, .env)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

from core.config import load_bot_env

BOT_DIR = Path(__file__).resolve().parent


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class ZakolConfig(BaseModel):
    """Параметры robot_zakol из окружения."""

    api_key: str = Field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = Field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    testnet: bool = Field(default_factory=lambda: _env_bool("TESTNET", False))
    category: str = Field(default_factory=lambda: os.getenv("CATEGORY", "linear"))
    symbol: str = Field(default_factory=lambda: os.getenv("SYMBOL", "BTCUSDT"))
    deposit_usd: float = Field(default_factory=lambda: _env_float("DEPOSIT_USD", 100.0))
    fixed_qty: float = Field(default_factory=lambda: _env_float("QTY", 0.0))
    position_pct: float = Field(
        default_factory=lambda: _env_float("POSITION_PCT", 10.0)
    )
    offset_pct: float = Field(default_factory=lambda: _env_float("OFFSET_PCT", 0.03))
    ttl_sec: float = Field(default_factory=lambda: _env_float("TTL_SEC", 20.0))
    min_price_change: float = Field(
        default_factory=lambda: _env_float("MIN_PRICE_CHANGE", 0.003),
    )
    stop_pct: float = Field(default_factory=lambda: _env_float("STOP_PCT", 0.02))
    trail_pct: float = Field(default_factory=lambda: _env_float("TRAIL_PCT", 0.02))
    be_trigger_pct: float = Field(
        default_factory=lambda: _env_float("BE_TRIGGER", 0.005)
    )
    be_offset_pct: float = Field(default_factory=lambda: _env_float("BE_OFFSET", 0.002))
    max_loss_usd: float = Field(
        default_factory=lambda: _env_float("MAX_LOSS_USD", 10.0)
    )
    max_drawdown_pct: float = Field(
        default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", 0.05),
    )
    partial_fill_pct: float = Field(
        default_factory=lambda: _env_float("PARTIAL_FILL_PCT", 0.80),
    )
    max_concurrent: int = Field(default_factory=lambda: _env_int("MAX_CONCURRENT", 1))
    requests_per_second: float = Field(
        default_factory=lambda: _env_float("REQUESTS_PER_SECOND", 100.0),
    )
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file: str = Field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/zakol.log")
    )
    state_file: str = Field(
        default_factory=lambda: os.getenv("STATE_FILE", "data/zakol_state.json"),
    )
    ws_enabled: bool = Field(default_factory=lambda: _env_bool("WS_ENABLED", True))

    @field_validator("symbol")
    @classmethod
    def _symbol_required(cls, v: str) -> str:
        if not v:
            raise ValueError("SYMBOL не задан")
        return v

    @field_validator("offset_pct")
    @classmethod
    def _offset_positive(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError("OFFSET_PCT должен быть в (0, 1)")
        return v

    @field_validator("ttl_sec")
    @classmethod
    def _ttl_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("TTL_SEC должен быть > 0")
        return v

    def validate_for_live(self) -> None:
        """Проверить обязательные поля перед live-запуском."""
        if not self.api_key or not self.api_secret:
            raise ValueError("BYBIT_API_KEY и BYBIT_API_SECRET обязательны")
        if self.fixed_qty <= 0 and self.deposit_usd <= 0:
            raise ValueError("нужен QTY > 0 или DEPOSIT_USD > 0")
        if self.max_concurrent < 1:
            raise ValueError("MAX_CONCURRENT должен быть >= 1")

    def order_notional(self) -> float:
        """Notional одной сделки в USDT (от депозита или фикс. QTY*цена не здесь)."""
        if self.fixed_qty > 0:
            return 0.0
        return self.deposit_usd * (self.position_pct / 100.0)


def load_config() -> ZakolConfig:
    """Загрузить .env бота и собрать конфигурацию."""
    load_bot_env(BOT_DIR)
    return ZakolConfig()
