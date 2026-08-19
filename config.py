"""Загрузка конфигурации бота из переменных окружения (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта — родительская папка этого файла.
# Используется для построения абсолютных путей к файлам состояния и логам.
PROJECT_ROOT = Path(__file__).resolve().parent

# Загружаем переменные из .env в окружение процесса.
# override=False — не перезаписываем уже существующие переменные ОС.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _get_bool(name: str, default: bool) -> bool:
    """Прочитать переменную окружения как boolean (true/false/1/0)."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    """Прочитать переменную окружения как число с плавающей точкой."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return float(value)


def _get_int(name: str, default: int) -> int:
    """Прочитать переменную окружения как целое число."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


@dataclass
class Config:
    """Все настройки бота.

    Каждое поле читается из переменной окружения (см. .env.example).
    """

    # --- Bybit API ---
    api_key: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    api_secret: str = field(
        default_factory=lambda: os.getenv("BYBIT_API_SECRET", ""),
    )
    testnet: bool = field(default_factory=lambda: _get_bool("TESTNET", True))

    # --- Режимы работы ---
    # simulation=True: ордера не отправляются, только логируются.
    simulation_mode: bool = field(
        default_factory=lambda: _get_bool("SIMULATION_MODE", True),
    )

    # --- Торговые параметры ---
    category: str = field(default_factory=lambda: os.getenv("CATEGORY", "linear"))
    symbol: str = field(default_factory=lambda: os.getenv("SYMBOL", ""))
    timeframe: str = field(default_factory=lambda: os.getenv("TIMEFRAME", "15"))
    fast_ma_period: int = field(
        default_factory=lambda: _get_int("FAST_MA_PERIOD", 7),
    )
    slow_ma_period: int = field(
        default_factory=lambda: _get_int("SLOW_MA_PERIOD", 25),
    )
    poll_interval: int = field(
        default_factory=lambda: _get_int("POLL_INTERVAL", 60),
    )
    kline_limit: int = field(
        default_factory=lambda: _get_int("KLINE_LIMIT", 200),
    )
    # Максимум REST-запросов в секунду (защита от блокировки по лимитам API)
    requests_per_second: float = field(
        default_factory=lambda: _get_float("REQUESTS_PER_SECOND", 10.0),
    )

    # --- Риск-менеджмент ---
    position_pct: float = field(
        default_factory=lambda: _get_float("POSITION_PCT", 10.0),
    )
    stop_loss_pct: float = field(
        default_factory=lambda: _get_float("STOP_LOSS_PCT", 2.0),
    )
    take_profit_pct: float = field(
        default_factory=lambda: _get_float("TAKE_PROFIT_PCT", 4.0),
    )

    # --- WebSocket ---
    ws_enabled: bool = field(default_factory=lambda: _get_bool("WS_ENABLED", True))

    # --- Telegram уведомления (опционально) ---
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
    telegram_chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    # --- Логирование и состояние ---
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "logs/bot.log"))
    state_file: str = field(
        default_factory=lambda: os.getenv("STATE_FILE", "data/state.json"),
    )

    # --- Инфраструктура ---
    # Папки для логов и данных (создаются при первом использовании)
    logs_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")

    @property
    def state_path(self) -> Path:
        """Абсолютный путь к файлу состояния."""
        return PROJECT_ROOT / self.state_file

    def validate(self) -> None:
        """Проверить обязательные настройки до запуска бота.

        Выбрасывает ValueError с понятным описанием, если чего-то не хватает.
        """
        if not self.api_key or not self.api_secret:
            raise ValueError(
                "BYBIT_API_KEY и BYBIT_API_SECRET обязательны. "
                "Скопируйте .env.example в .env и заполните ключи.",
            )
        if not self.symbol:
            raise ValueError(
                "SYMBOL не задан. Укажите инструмент в .env, например: SYMBOL=ETHUSDT",
            )
        if self.category not in ("spot", "linear"):
            raise ValueError("CATEGORY должен быть 'spot' или 'linear'.")
        if self.fast_ma_period >= self.slow_ma_period:
            raise ValueError(
                "FAST_MA_PERIOD должен быть меньше SLOW_MA_PERIOD "
                "(иначе пересечение средних не имеет смысла).",
            )
        if not (0 < self.position_pct <= 100):
            raise ValueError("POSITION_PCT должен быть в диапазоне (0, 100].")
        if self.stop_loss_pct <= 0 or self.take_profit_pct <= 0:
            raise ValueError("STOP_LOSS_PCT и TAKE_PROFIT_PCT должны быть > 0.")
