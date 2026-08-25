# SESSION_NOTES.md

## Статус: реструктуризация завершена, стратегии переписаны с нуля

### ВАЖНО — договорённость с пользователем
- Ботов НЕ запускать. Пользователь сделает НОВЫЕ тестовые ключи Bybit,
  затем прогоняем ботов на тестнете, не трогая реальный счёт.
- Все 5 ботов сейчас в SIMULATION_MODE=false, TESTNET=true.
- Ключи тестнета уже в .env файлах.

### Что сделано
- Удалены старые боты: bot_sma, bot_combo, bot_adaptive, bot_swings
- Созданы новые боты:
  - `bot_flat` — боковик (рип-сайдинг, консолидации)
  - `bot_yrovni` — уровни (пробой + ретест)
  - `bot_trend` — тренд (следование за трендом)
  - `bot_impulse` — импульс (моментум, быстрые входы)
  - `bot_0` — нулевой (минимальная стратегия)
- Стратегии: пока заглушки (hold), нужно писать логику
- Ядро `core/` не тронуто: engine, strategies, indicators, config, bybit_client, risk_manager, state, notifier, metrics, logger, utils
- Тесты: 60 пройдено (core + индикаторы + риск-менеджер)
- backtest.py: choices обновлены, но make_strategy() выбрасывает ValueError (стратегии не реализованы)
- launcher.py: default bot = bot_flat
- docker-compose.yml: 5 сервисов (bot-flat, bot-yrovni, bot-trend, bot-impulse, bot-0)
- README.md обновлён

### Следующие шаги
- Написать стратегии для каждого бота (strategy.py)
- Раскомментировать импорты в backtest.py
- Прогнать backtest каждой стратегии
- Прогнать ботов на тестнете
