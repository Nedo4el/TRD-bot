# SESSION_NOTES.md

## Статус: инфраструктура готова, стратегии пустые (заглушки)

### ВАЖНО — договорённость с пользователем
- Все 5 ботов в SIMULATION_MODE=false, TESTNET=true.
- Тестовые ключи Bybit: qKsYtQhn8JLKmfdOkI (testnet)
- Ботов НЕ запускать — пользователь скажет когда.
- Стратегии переписываем с нуля — пользователь сам решает что в каждом боте.

### Что сделано (текущая сессия)
- Проверили техническое состояние:
  - core модули: 11/11 OK
  - стратегии: 4/4 OK (заглушки)
  - main.py: 4/4 OK (asyncio loop стартует)
  - конфиги: 4/4 OK (ключ загружен)
  - Bybit testnet: OK (баланс=0, свечи XRPUSDT, позиция=None)
  - RiskManager: OK (FIXED_QTY + процентный)
  - State: OK (save/load/clear/dedup)
  - Индикаторы: OK (SMA, EMA, RSI, BB, ATR)
  - pytest: 60/60 → потом 26/26 (после удаления старых тестов)
  - ruff: чисто
- Удалены старые боты: bot_sma, bot_combo, bot_adaptive, bot_swings
- Удалены старые тесты: test_strategies.py, test_swings_extended.py
- Созданы новые боты (шаблоны с заглушками):
  - bot_flat — боковик (рип-сайдинг, консолидации), M5, SL 0.3% / TP 0.5%
  - bot_yrovni — уровни (пробой + ретест), M5, SL 0.2% / TP 0.4%
  - bot_trend — тренд (следование за трендом), M5, SL 2% / TP 4%
  - bot_impulse — импульс (моментум), M1, SL 0.3% / TP 0.5%
  - bot_0 — нулевой (минимальная), M5, SL 2% / TP 4%
- Каждый бот: main.py + strategy.py (заглушка hold) + .env + .env.example
- Обновлены: launcher.py (default=bot_flat), docker-compose.yml (5 сервисов)
- backtest.py: choices=[flat, yrovni, trend, impulse, zero], make_strategy() пока ValueError
- Запушено: b52be9a на main

### Текущая структура проекта
```
TRD bot/
├── core/                  # Общий движок (11 модулей, не трогаем)
│   ├── engine.py          # TradingBot + run_bot()
│   ├── strategies.py      # BaseStrategy ABC + Signal
│   ├── indicators.py      # SMA, EMA, RSI, Bollinger, ATR
│   ├── config.py          # Config dataclass + load_bot_env
│   ├── bybit_client.py    # REST + WS (pybit)
│   ├── risk_manager.py    # Position sizing
│   ├── state.py           # StateStore (JSON)
│   ├── notifier.py        # Telegram
│   ├── metrics.py         # Счётчики
│   ├── logger.py          # Логи
│   └── utils.py           # Retry, backoff
├── bot_flat/              # Боковик
├── bot_yrovni/            # Уровни
├── bot_trend/             # Тренд
├── bot_impulse/           # Импульс
├── bot_0/                 # Нулевой
├── backtest.py            # Бэктест (ждёт стратегий)
├── launcher.py            # Фоновый запуск
├── sync_github.ps1        # Автосинхронизация (30 мин)
├── tests/                 # 26 тестов (core + индикаторы + риск)
├── .env                   # Общий (fallback для backtest)
└── docker-compose.yml     # 5 сервисов
```

### Файлы бота (на примере bot_flat)
- `bot_flat/__init__.py` — пустой
- `bot_flat/main.py` — точка входа, создаёт стратегию и вызывает run_bot()
- `bot_flat/strategy.py` — FlatStrategy(BaseStrategy), заглушка hold
- `bot_flat/.env` — API ключи, тестнет, настройки (ключ уже вписан)
- `bot_flat/.env.example` — шаблон без ключей

### API сигнатуры (для справки при написании стратегий)
- `Config()` — dataclass, читает из os.environ (после load_bot_env)
- `BybitClient(config)` — .get_klines(symbol, interval, limit), .get_price(symbol), .get_balance(), .get_position(symbol) — все async
- `RiskManager(config)` — .calculate_position_size(balance, price) -> float
- `RiskManager(config).build_plan(side, balance, price) -> PositionPlan`
- `StateStore(path)` — async: .set_position(pos), .clear_position(symbol)
- `Signal(action, reason, stop_loss=None, take_profit=None)` — action: "buy"|"sell"|"hold"
- `BaseStrategy.check_signal(candles: list[Candle]) -> Signal`
- `Candle(open_time, open, high, low, close, volume)`

### Индикаторы (core/indicators.py)
- `sma(prices, period)` -> list[float]
- `ema(prices, period)` -> list[float]
- `rsi(prices, period)` -> list[float]
- `bollinger(prices, period, deviation)` -> tuple[upper, middle, lower]
- `atr(highs, lows, closes, period)` -> list[float]
- `crossed_up(fast, slow)` -> bool
- `crossed_down(fast, slow)` -> bool

### Следующие шаги
- Написать стратегии для каждого бота (strategy.py)
- Раскомментировать импорты в backtest.py и make_strategy()
- Прогнать backtest каждой стратегии на истории XRPUSDT
- Добавить тесты для стратегий
- Прогнать ботов на тестнете (по команде пользователя)
