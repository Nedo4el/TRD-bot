"""Конфигурация robot_trend (pydantic, .env). Только технические параметры."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from core.config import load_bot_env

BOT_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


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


class TrendConfig(BaseModel):
    """Параметры robot_trend из окружения."""

    # --- Доступ и инструмент ---
    api_key: str = Field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = Field(default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""))
    testnet: bool = Field(default_factory=lambda: _env_bool("TESTNET", False))
    category: str = Field(default_factory=lambda: os.getenv("CATEGORY", "linear"))
    symbol: str = Field(default_factory=lambda: os.getenv("SYMBOL", "BTCUSDT"))
    timeframe: str = Field(default_factory=lambda: os.getenv("TIMEFRAME", "5"))
    # старший ТФ: направление + сила тренда (EMA/ADX), входы только по нему
    htf_timeframe: str = Field(default_factory=lambda: os.getenv("HTF_TIMEFRAME", "30"))
    htf_candle_warmup: int = Field(default_factory=lambda: _env_int("HTF_WARMUP", 250))

    # --- Размер позиции ---
    deposit_usd: float = Field(default_factory=lambda: _env_float("DEPOSIT_USD", 100.0))
    fixed_qty: float = Field(default_factory=lambda: _env_float("QTY", 0.0))
    position_pct: float = Field(
        default_factory=lambda: _env_float("POSITION_PCT", 10.0)
    )

    # --- Свечи ---
    candles_warmup: int = Field(default_factory=lambda: _env_int("CANDLE_WARMUP", 300))
    poll_sec: float = Field(default_factory=lambda: _env_float("POLL_SEC", 10.0))

    # --- Защита (kill-switch по equity) ---
    max_loss_usd: float = Field(
        default_factory=lambda: _env_float("MAX_LOSS_USD", 10.0)
    )
    max_drawdown_pct: float = Field(
        default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", 0.05)
    )

    # --- Допуски, паузы, дневной стоп-кран ---
    slippage_pct: float = Field(
        default_factory=lambda: _env_float("SLIPPAGE_PCT", 0.005)
    )
    cooldown_sec: float = Field(
        default_factory=lambda: _env_float("COOLDOWN_SEC", 15.0)
    )
    daily_loss_limit: float = Field(
        default_factory=lambda: _env_float("DAILY_LOSS_LIMIT", 0.0)
    )
    max_trades_per_day: int = Field(
        default_factory=lambda: _env_int("MAX_TRADES_PER_DAY", 0)
    )

    # --- Устойчивость WS ---
    reconnect_sec: float = Field(
        default_factory=lambda: _env_float("RECONNECT_SEC", 2.0)
    )
    heartbeat_sec: float = Field(
        default_factory=lambda: _env_float("HEARTBEAT_SEC", 1.0)
    )

    # --- Подпись запросов Bybit ---
    recv_window: int = Field(default_factory=lambda: _env_int("RECV_WINDOW", 10000))
    time_sync: bool = Field(default_factory=lambda: _env_bool("TIME_SYNC", True))

    # --- Ручная остановка файлом (пусто = выключено) ---
    kill_switch_file: str = Field(
        default_factory=lambda: os.getenv("KILL_SWITCH", "data/trend.kill")
    )
    # --- Идемпотентность ордеров: префикс orderLinkId (Bybit: макс. 36 симв.) ---
    order_link_prefix: str = Field(
        default_factory=lambda: os.getenv("ORDER_LINK_ID_PREFIX", "tr-")
    )

    # --- Фандинг: не входить в окно перед финансированием (perpetual) ---
    funding_aware: bool = Field(
        default_factory=lambda: _env_bool("FUNDING_AWARE", True)
    )
    funding_window_sec: float = Field(
        default_factory=lambda: _env_float("FUNDING_WINDOW_SEC", 60.0)
    )

    # --- Служебное ---
    requests_per_second: float = Field(
        default_factory=lambda: _env_float("REQUESTS_PER_SECOND", 100.0),
    )
    log_level: str = Field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file: str = Field(
        default_factory=lambda: os.getenv("LOG_FILE", "logs/trend.log")
    )
    state_file: str = Field(
        default_factory=lambda: os.getenv("STATE_FILE", "data/trend_state.json"),
    )
    ws_enabled: bool = Field(default_factory=lambda: _env_bool("WS_ENABLED", True))

    @field_validator("symbol")
    @classmethod
    def _symbol_required(cls, v: str) -> str:
        if not v:
            raise ValueError("SYMBOL не задан")
        return v

    @field_validator("timeframe", "htf_timeframe")
    @classmethod
    def _timeframe_positive(cls, v: str) -> str:
        if not v.isdigit() or int(v) <= 0:
            raise ValueError("TIMEFRAME должен быть положительным числом (минуты)")
        return v

    @model_validator(mode="after")
    def _htf_slower_than_tf(self) -> TrendConfig:
        if int(self.htf_timeframe) <= int(self.timeframe):
            raise ValueError("HTF_TIMEFRAME должен быть больше TIMEFRAME")
        return self

    @field_validator("position_pct")
    @classmethod
    def _position_pct_range(cls, v: float) -> float:
        if not (0 < v <= 100):
            raise ValueError("POSITION_PCT должен быть в (0, 100]")
        return v

    @field_validator("slippage_pct")
    @classmethod
    def _slippage_range(cls, v: float) -> float:
        if not (0 <= v < 1):
            raise ValueError("SLIPPAGE_PCT должен быть в [0, 1)")
        return v

    @field_validator(
        "cooldown_sec",
        "funding_window_sec",
        "daily_loss_limit",
        "max_loss_usd",
        "max_drawdown_pct",
    )
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("значение не может быть < 0")
        return v

    @field_validator("reconnect_sec", "heartbeat_sec", "poll_sec")
    @classmethod
    def _seconds_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("значение в секундах должно быть > 0")
        return v

    @field_validator("max_trades_per_day", "candles_warmup", "htf_candle_warmup")
    @classmethod
    def _int_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("значение не может быть < 0")
        return v

    @field_validator("recv_window")
    @classmethod
    def _recv_window_range(cls, v: int) -> int:
        if not (0 < v <= 50000):
            raise ValueError("RECV_WINDOW должен быть в (0, 50000] мс")
        return v

    @field_validator("order_link_prefix")
    @classmethod
    def _link_prefix_len(cls, v: str) -> str:
        # orderLinkId: префикс + 16 hex <= 36 символов Bybit
        if not 1 <= len(v) <= 17:
            raise ValueError("ORDER_LINK_ID_PREFIX: длина 1..17 символов")
        return v

    def validate_for_live(self) -> None:
        """Проверить обязательные поля перед live-запуском."""
        if not self.api_key or not self.api_secret:
            raise ValueError("BYBIT_API_KEY и BYBIT_API_SECRET обязательны")
        if self.category != "linear":
            raise ValueError(
                "CATEGORY должен быть linear (perpetual) — спот запрещён",
            )
        if self.fixed_qty <= 0 and self.deposit_usd <= 0:
            raise ValueError("нужен QTY > 0 или DEPOSIT_USD > 0")
        if not self.ws_enabled:
            raise ValueError(
                "WS_ENABLED должен быть true для live (филлы идут по WS)",
            )
        if self.candles_warmup < 1:
            raise ValueError("CANDLE_WARMUP должен быть >= 1")
        if self.testnet:
            logger.warning("TESTNET=true — работа на тестнете")

    def order_notional(self) -> float:
        """Notional одной сделки в USDT (0 — считаем от фикс. QTY)."""
        if self.fixed_qty > 0:
            return 0.0
        return self.deposit_usd * (self.position_pct / 100.0)


def load_config() -> TrendConfig:
    """Загрузить .env бота и собрать конфигурацию."""
    load_bot_env(BOT_DIR)
    return TrendConfig()
