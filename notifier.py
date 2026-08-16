"""Telegram-уведомления о сделках, ошибках и критических событиях.

Если TOKEN/CHAT_ID не настроены (пустые строки в .env), уведомления
бесшумно отключаются — всё пишется только в лог.
"""

from __future__ import annotations

import logging

import aiohttp

from config import Config

logger = logging.getLogger(__name__)

# Официальный endpoint Telegram Bot API для отправки сообщений
TG_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    """Отправка уведомлений в Telegram (асинхронно, через aiohttp)."""

    def __init__(self, config: Config) -> None:
        self._token = config.telegram_bot_token.strip()
        self._chat_id = config.telegram_chat_id.strip()
        # Если настройки пустые — уведомления отключены
        self.enabled = bool(self._token and self._chat_id)
        if not self.enabled:
            logger.info(
                "Telegram-уведомления отключены "
                "(заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env)",
            )

    async def notify(self, text: str) -> None:
        """Отправить сообщение в Telegram.

        Args:
            text: текст сообщения (не длиннее ~4096 символов — лимит API).

        Ошибки отправки не роняют бота: логируем и продолжаем.
        """
        if not self.enabled:
            logger.info("[notify] %s", text)
            return

        url = TG_API_URL.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as response,
            ):
                if response.status != 200:
                    logger.error(
                        "Telegram sendMessage вернул %s: %s",
                        response.status,
                        await response.text(),
                    )
        except aiohttp.ClientError as exc:
            logger.error("Ошибка отправки Telegram-уведомления: %s", exc)
        except TimeoutError:
            logger.error("Таймаут отправки Telegram-уведомления")

    # --- Удобные обёртки для типовых событий ---

    async def trade_opened(
        self, symbol: str, side: str, qty: float, price: float
    ) -> None:
        """Уведомить об открытии позиции."""
        await self.notify(
            f"\U0001f4c8 ОТКРЫТА позиция {symbol}\n"
            f"Сторона: {side}\nРазмер: {qty:.8g}\nЦена: {price:.8g}",
        )

    async def trade_closed(
        self, symbol: str, qty: float, price: float, pnl: float
    ) -> None:
        """Уведомить о закрытии позиции (с итоговой прибылью/убытком)."""
        await self.notify(
            f"\U0001f4c9 ЗАКРЫТА позиция {symbol}\n"
            f"Размер: {qty:.8g}\nЦена: {price:.8g}\n"
            f"PnL: {pnl:+.4f} USDT",
        )

    async def error(self, message: str) -> None:
        """Уведомить об ошибке."""
        await self.notify(f"\u26a0\ufe0f ОШИБКА: {message}")
