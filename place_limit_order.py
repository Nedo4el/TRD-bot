"""Quick script to place limit buy order at 5% below current price."""
import asyncio
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_PROJECT_ROOT))

from core.bybit_client import BybitClient
from core.config import Config, load_bot_env


async def main() -> None:
    load_bot_env(_PROJECT_ROOT / "robot_flat")
    config = Config()
    client = BybitClient(config)

    try:
        # Получаем текущую цену
        price = await client.get_price("BTRUSDT")
        limit_price = round(price * 0.95, 5)
        print(f"Текущая цена BTRUSDT: {price}")
        print(f"Лимитный ордер (5% ниже): {limit_price}")

        # Получаем шаг qty
        qty_step = await client._get_qty_step("BTRUSDT")
        print(f"Qty step: {qty_step}")

        # Ставим лимитный ордер на покупку (минимум 5 USDT)
        min_qty = int(5.0 / limit_price / qty_step) * qty_step + qty_step
        qty = max(min_qty, 110.0)  # 110 * 0.048 ~ 5.3 USDT
        print(f"Размещаем лимитный ордер: Buy {qty} @ {limit_price}...")

        order = await client.place_order(
            symbol="BTRUSDT",
            side="Buy",
            qty=qty,
            order_type="Limit",
            price=limit_price,
        )
        print(f"Ордер размещён: {order}")

    except Exception as e:
        print(f"Ошибка: {e}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
