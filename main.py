"""Точка входа торгового бота.

Главный цикл:
1. Подключаемся к Bybit (REST + опционально WebSocket).
2. Каждые POLL_INTERVAL секунд получаем свечи и считаем сигнал стратегии.
3. Нет позиции  -> сигнал buy/sell -> открываем сделку через риск-менеджер.
4. Есть позиция -> для спота следим за SL/TP на клиенте,
   для фьючерсов SL/TP выставлены на бирже (позиция закроется сама).
5. При сбоях — повторные попытки, а при длинной серии ошибок
   пересоздаём HTTP-сессию (переподключение).

Режимы:
- SIMULATION_MODE=true  — ордера не отправляются, только логируются;
- TESTNET=true          — реальные ордера, но на тестовой сети;
- TESTNET=false         — реальные ордера на mainnet.
"""

from __future__ import annotations

import asyncio

from bybit_client import BybitClient
from config import Config
from logger import get_logger, setup_logging
from metrics import Metrics
from notifier import Notifier
from risk_manager import RiskManager
from state import StateStore, StoredPosition
from strategy import generate_signal
from utils import exponential_backoff

logger = get_logger(__name__)

# После скольких сбоев подряд пересоздаём HTTP-сессию
RECONNECT_AFTER_ERRORS = 5
# Как часто выводить сводку метрик (в итерациях главного цикла)
METRICS_REPORT_EVERY = 60


class TradingBot:
    """Торговый бот: оркестрация клиента, стратегии и риск-менеджмента."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = BybitClient(config)
        self.risk = RiskManager(config)
        self.notifier = Notifier(config)
        self.state = StateStore(config.state_path)
        self.metrics = Metrics()
        self._consecutive_errors = 0

    # ======================= Вспомогательные шаги =======================

    async def _get_market_context(self) -> tuple[list, float]:
        """Получить свечи и текущую цену одним вызовом.

        Returns:
            Кортеж (candles, price).
        """
        candles = await self.client.get_klines(
            self.config.symbol,
            interval=self.config.timeframe,
            limit=self.config.kline_limit,
        )
        # Если WebSocket уже отдал свежую цену — используем её,
        # иначе берём цену из REST (одним запросом меньше).
        price = self.client.last_ws_price.get(self.config.symbol)
        if price is None:
            price = await self.client.get_price(self.config.symbol)
        return candles, price

    async def _open_position(self, side: str, price: float) -> None:
        """Открыть позицию: размер из риск-менеджера, ордер, SL/TP.

        Args:
            side: "Buy" (long) или "Sell" (short).
            price: текущая цена для расчёта размеров.
        """
        balance = await self.client.get_balance()
        plan = self.risk.build_plan(balance, price, side)

        if self.config.simulation_mode:
            # --- Режим симуляции: ничего не отправляем на биржу ---
            logger.info(
                "[SIMULATION] Открыли бы %s %s: qty=%.8g @ %.8g, SL=%.8g, TP=%.8g",
                side,
                self.config.symbol,
                plan.qty,
                plan.entry_price,
                plan.stop_loss,
                plan.take_profit,
            )
            await self.notifier.notify(
                f"[SIMULATION] Сигнал {side} {self.config.symbol} "
                f"(qty={plan.qty:.8g} @ {plan.entry_price:.8g})",
            )
            self.metrics.record_trade_open()
            return

        # --- Реальный режим: отправляем рыночный ордер ---
        order = await self.client.place_order(
            symbol=self.config.symbol,
            side=side,
            qty=plan.qty,
            order_type="Market",
        )
        order_id = order["result"]["orderId"]
        logger.info(
            "Открыта позиция %s %s: qty=%.8g @ %.8g (ордер %s)",
            side,
            self.config.symbol,
            plan.qty,
            plan.entry_price,
            order_id,
        )

        # Выставляем SL/TP на бирже (работает для фьючерсов).
        # Для спота Bybit вернёт ошибку — мониторинг будет на клиенте.
        try:
            await self.client.set_stop_loss_take_profit(
                symbol=self.config.symbol,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Серверные SL/TP не выставлены (%s) — мониторинг на клиенте",
                exc,
            )

        # Сохраняем позицию в состояние (переживёт рестарт бота)
        await self.state.set_position(
            StoredPosition(
                symbol=self.config.symbol,
                side=side,
                qty=plan.qty,
                entry_price=plan.entry_price,
                stop_loss=plan.stop_loss,
                take_profit=plan.take_profit,
            ),
        )
        self.metrics.record_trade_open()
        await self.notifier.trade_opened(
            self.config.symbol,
            side,
            plan.qty,
            plan.entry_price,
        )

    async def _close_position(
        self,
        stored: StoredPosition,
        price: float,
        reason: str,
    ) -> None:
        """Закрыть позицию (по SL/TP, по сигналу или вручную).

        Args:
            stored: позиция из хранилища состояния.
            price: текущая цена.
            reason: причина закрытия (пишется в лог и метрики).
        """
        if self.config.simulation_mode:
            logger.info("[SIMULATION] Закрыли бы %s: %s", stored.symbol, reason)
            await self.state.clear_position(stored.symbol)
            return

        await self.client.close_position(
            symbol=stored.symbol,
            qty=stored.qty,
            side=stored.side,
        )
        # Прибыль: (цена - вход) * qty для long, наоборот для short
        if stored.side == "Buy":
            pnl = (price - stored.entry_price) * stored.qty
        else:
            pnl = (stored.entry_price - price) * stored.qty
        logger.info(
            "Закрыта позиция %s: %s, цена %.8g, PnL %+.4f USDT",
            stored.symbol,
            reason,
            price,
            pnl,
        )
        await self.state.clear_position(stored.symbol)
        self.metrics.record_trade_close(pnl)
        await self.notifier.trade_closed(stored.symbol, stored.qty, price, pnl)

    async def _check_open_position(self, stored: StoredPosition, price: float) -> None:
        """Обработать открытую позицию: закрытие по SL/TP или сигналу.

        Для фьючерсов SL/TP живут на бирже — проверяем, не закрылась ли
        позиция там. Для спота мониторим цены на клиенте.
        """
        symbol = self.config.symbol

        if self.config.category != "spot":
            # Фьючерсы: сервер сам закроет позицию по SL/TP.
            # Если позиции больше нет на бирже — она закрылась.
            position = await self.client.get_position(symbol)
            if position is None:
                await self._close_position(stored, price, "закрыта на бирже (SL/TP)")
            return

        # --- Спот: клиентский мониторинг стоп-уровней ---
        if stored.side == "Buy":
            if price <= stored.stop_loss:
                await self._close_position(stored, price, "стоп-лосс (клиент)")
                return
            if price >= stored.take_profit:
                await self._close_position(stored, price, "тейк-профит (клиент)")
                return
        else:
            if price >= stored.stop_loss:
                await self._close_position(stored, price, "стоп-лосс (клиент)")
                return
            if price <= stored.take_profit:
                await self._close_position(stored, price, "тейк-профит (клиент)")
                return

    # ========================= Главный цикл =========================

    async def _sync_state_with_exchange(self) -> None:
        """Восстановить локальное состояние из реальной позиции на бирже.

        Нужно, если бот упал/перезапустился, а state-файл не сохранился:
        без этого бот может открыть дублирующую позицию поверх уже открытой.
        """
        if self.state.get_position(self.config.symbol) is not None:
            return

        position = await self.client.get_position(self.config.symbol)
        if position is None:
            return

        await self.state.set_position(
            StoredPosition(
                symbol=position.symbol,
                side=position.side,
                qty=position.size,
                entry_price=position.avg_price,
                stop_loss=position.stop_loss or 0.0,
                take_profit=position.take_profit or 0.0,
            ),
        )
        logger.info(
            "Восстановлена позиция с биржи: %s %s qty=%.8g @ %.8g",
            position.side,
            position.symbol,
            position.size,
            position.avg_price,
        )

    async def run(self) -> None:
        """Запустить основной цикл торговли (работает до остановки)."""
        logger.info(
            "Бот стартует: %s @ %s, %s, интервал %dс, testnet=%s, simulation=%s",
            self.config.symbol,
            self.config.timeframe,
            self.config.category,
            self.config.poll_interval,
            self.config.testnet,
            self.config.simulation_mode,
        )

        if self.config.ws_enabled:
            self.client.start_ws()

        # Восстанавливаем позицию с биржи, если state потерян (упал процесс).
        # Делаем до цикла, чтобы не открыть дубль поверх существующей сделки.
        try:
            await self._sync_state_with_exchange()
        except Exception:
            logger.exception("Не удалось синхронизировать состояние с биржей")

        iteration = 0
        while True:
            try:
                candles, price = await self._get_market_context()
                self._consecutive_errors = 0

                # --- Расчёт сигнала стратегии по последним свечам ---
                signal = generate_signal(
                    candles,
                    self.config.fast_ma_period,
                    self.config.slow_ma_period,
                )
                logger.debug(
                    "Сигнал: %s (%s), цена %.8g",
                    signal.action,
                    signal.reason,
                    price,
                )

                stored = self.state.get_position(self.config.symbol)

                if stored is None:
                    # --- Нет позиции: ждём сигнал на вход ---
                    if signal.action == "buy":
                        await self._open_position("Buy", price)
                    elif signal.action == "sell" and self.config.category != "spot":
                        # Короткая позиция возможна только на фьючерсах
                        await self._open_position("Sell", price)
                    else:
                        logger.debug("Сигнал hold — ждём дальше")
                else:
                    # --- Позиция открыта: управляем выходом ---
                    await self._check_open_position(stored, price)

            except Exception:  # catch-all: главный цикл не должен умирать
                # Обработка сбоев: логируем, увеличиваем счётчик ошибок,
                # при длинной серии — пересоздаём HTTP-сессию.
                self._consecutive_errors += 1
                self.metrics.record_api_error()
                logger.exception("Ошибка в главном цикле")

                if self._consecutive_errors >= RECONNECT_AFTER_ERRORS:
                    logger.warning(
                        "Серия из %d ошибок — переподключаюсь к Bybit",
                        self._consecutive_errors,
                    )
                    self.client.reconnect()
                    self._consecutive_errors = 0
                await asyncio.sleep(exponential_backoff(self._consecutive_errors, 5.0))

            # --- Периодический отчёт по метрикам ---
            iteration += 1
            if iteration % METRICS_REPORT_EVERY == 0:
                logger.info("Метрики:\n%s", self.metrics.report())

            await asyncio.sleep(self.config.poll_interval)


async def main() -> None:
    """Запуск бота: конфиг -> лог -> валидация -> цикл."""
    config = Config()
    setup_logging(config.log_file, config.log_level)
    config.validate()

    bot = TradingBot(config)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Остановка по запросу пользователя...")
    finally:
        # Корректное завершение: закрываем соединения и сохраняем состояние
        bot.client.close()
        await bot.state.save()
        logger.info("Бот остановлен. Метрики:\n%s", bot.metrics.report())


if __name__ == "__main__":
    asyncio.run(main())
