"""Управление рисками: размер позиции, стоп-лосс, тейк-профит.

Основные принципы:
- рискуем только заранее заданным процентом баланса на одну сделку;
- стоп-лосс ограничивает убыток, тейк-профит фиксирует прибыль;
- все расчёты — чистые функции, легко тестировать.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import Config


@dataclass
class PositionPlan:
    """План сделки, рассчитанный риск-менеджером."""

    qty: float  # размер позиции в базовом активе
    entry_price: float  # ориентировочная цена входа
    stop_loss: float  # цена стоп-лосса
    take_profit: float  # цена тейк-профита


class RiskManager:
    """Расчёт параметров сделки на основе конфигурации рисков."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def calculate_position_size(self, balance: float, price: float) -> float:
        """Рассчитать размер позиции в базовом активе.

        Формула: (баланс * % на позицию) / цена инструмента.
        Пример: баланс 1000 USDT, POSITION_PCT=10% -> 100 USDT на сделку;
        при цене BTC 60 000 -> qty = 100 / 60000 = 0.00167 BTC.

        Args:
            balance: доступный баланс в USDT.
            price: текущая цена инструмента.

        Returns:
            Количество базового актива для ордера.
        """
        if balance <= 0 or price <= 0:
            raise ValueError("Баланс и цена должны быть положительными")
        amount_usdt = balance * (self.config.position_pct / 100.0)
        return amount_usdt / price

    def calculate_stop_loss(self, entry_price: float, side: str) -> float:
        """Рассчитать цену стоп-лосса.

        Для long стоп ниже цены входа, для short — выше.

        Args:
            entry_price: цена входа.
            side: сторона сделки ("Buy" или "Sell").

        Returns:
            Цена стоп-лосса.
        """
        pct = self.config.stop_loss_pct / 100.0
        if side == "Buy":  # long: стоп ниже входа
            return round(entry_price * (1 - pct), 8)
        return round(entry_price * (1 + pct), 8)  # short: стоп выше входа

    def calculate_take_profit(self, entry_price: float, side: str) -> float:
        """Рассчитать цену тейк-профита.

        Для long тейк выше цены входа, для short — ниже.

        Args:
            entry_price: цена входа.
            side: сторона сделки ("Buy" или "Sell").

        Returns:
            Цена тейк-профита.
        """
        pct = self.config.take_profit_pct / 100.0
        if side == "Buy":
            return round(entry_price * (1 + pct), 8)
        return round(entry_price * (1 - pct), 8)

    def build_plan(self, balance: float, entry_price: float, side: str) -> PositionPlan:
        """Собрать полный план сделки (размер + стопы).

        Args:
            balance: доступный баланс в USDT.
            entry_price: текущая цена (для рыночного входа).
            side: сторона сделки ("Buy" или "Sell").

        Returns:
            PositionPlan с размером, стоп-лоссом и тейк-профитом.
        """
        qty = self.calculate_position_size(balance, entry_price)
        return PositionPlan(
            qty=qty,
            entry_price=entry_price,
            stop_loss=self.calculate_stop_loss(entry_price, side),
            take_profit=self.calculate_take_profit(entry_price, side),
        )
