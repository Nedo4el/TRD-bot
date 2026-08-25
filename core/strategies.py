"""Базовый интерфейс торговой стратегии.

Как написать свою стратегию:
1. Создайте папку bot_<имя>/ и скопируйте туда main.py + strategy.py
   из любой существующей папки бота.
2. В strategy.py напишите класс, наследующийся от BaseStrategy,
   и реализуйте метод check_signal() — вся логика входа живёт там.
3. Числа для «тонкой настройки» вынесите в .env бота (см. main.py).

Движок (core/engine.py) сам делает всё остальное: свечи, ордера,
стопы, уведомления, восстановление после рестарта.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.bybit_client import Candle


@dataclass
class Signal:
    """Сигнал стратегии на текущей закрытой свече.

    Attributes:
        action: "buy" | "sell" | "hold".
        reason: человекочитаемое пояснение — попадает в лог и Telegram.
        stop_loss: абсолютная цена стоп-лосса, если стратегия считает её
            сама (например, от ATR). None -> возьмётся % из .env.
        take_profit: абсолютная цена тейк-профита (аналогично).
    """

    action: str
    reason: str
    stop_loss: float | None = None
    take_profit: float | None = None


class BaseStrategy(ABC):
    """Родитель всех стратегий.

    Наследник ОБЯЗАН реализовать check_signal().
    Стратегия — обычный объект: может хранить своё состояние между
    вызовами (например, список найденных уровней).
    """

    #: Короткое имя стратегии для логов ("sma", "combo", ...)
    name: str = "base"

    @abstractmethod
    def check_signal(self, candles: list[Candle]) -> Signal:
        """Проанализировать свечи и вернуть сигнал.

        Args:
            candles: закрытые свечи от старых к новым
                (последняя = последняя ЗАКРЫТАЯ, незакрытую движок убирает).

        Returns:
            Signal с действием и пояснением.
        """
