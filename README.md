# TRD Bot — набор торговых ботов для Bybit v5

Пять независимых ботов на общем движке. Общая логика (биржа, ордера,
стопы, Telegram, восстановление после рестарта) живёт в `core/`,
у каждого бота — только своя стратегия и свой `.env`.

```
TRD bot/
├── core/                  # Общий движок (менять не нужно)
│   ├── engine.py          # Главный цикл: свечи -> сигнал -> сделка
│   ├── strategies.py      # BaseStrategy — интерфейс стратегии
│   ├── indicators.py      # SMA, EMA, RSI, Bollinger, ATR (вручную)
│   ├── config.py          # Чтение .env
│   ├── bybit_client.py    # REST + WebSocket Bybit
│   ├── risk_manager.py    # Размер позиции, SL/TP
│   ├── state.py           # Сохранение позиций в JSON
│   ├── notifier.py        # Telegram-уведомления
│   ├── metrics.py         # Счётчики сделок и ошибок
│   ├── logger.py          # Логи: консоль + файл с ротацией
│   └── utils.py           # Retry, backoff
├── bot_flat/              # Боковик (рип-сайдинг, консолидации)
├── bot_yrovni/            # Уровни (пробой + ретест)
├── bot_trend/             # Тренд (следование за трендом)
├── bot_impulse/           # Импульс (моментум, быстрые входы)
├── bot_0/                 # Нулевой (минимальная стратегия)
├── backtest.py            # Бэктест любой стратегии
├── launcher.py            # Фоновый запуск/остановка бота
└── tests/                 # pytest-тесты стратегий и индикаторов
```

## Быстрый старт

```powershell
# 1. Зависимости
uv pip install -r requirements.txt

# 2. Настройки бота: скопировать пример и вписать ключи Bybit
Copy-Item bot_flat\.env.example bot_flat\.env
#    отредактировать BYBIT_API_KEY / BYBIT_API_SECRET

# 3. Запуск (безопасно: SIMULATION_MODE=true — без реальных ордеров)
python bot_flat/main.py
```

Каждый бот читает настройки ИЗ СВОЕЙ ПАПКИ (`bot_xxx/.env`).

## Боты и их параметры

Все «крутилки» вынесены в `.env` бота — код трогать не нужно.

| Бот | Идея |
|---|---|
| `bot_flat` | Боковик (рип-сайдинг, консолидации) |
| `bot_yrovni` | Уровни (пробой + ретест) |
| `bot_trend` | Тренд (следование за трендом) |
| `bot_impulse` | Импульс (моментум, быстрые входы) |
| `bot_0` | Нулевой (минимальная стратегия) |

Сигналы считаются только по ЗАКРЫТЫМ свечам; позиция всегда одна.

## Как добавить свою стратегию

1. Скопируйте папку любого бота, например `bot_flat` → `bot_mya`.
2. В `bot_mya/strategy.py` напишите класс на базе `BaseStrategy`:
   метод `check_signal(candles) -> Signal` — единственное,
   что обязательно. Индикаторы берите из `core/indicators.py`.
3. В `bot_mya/.env` задайте свои числа.
4. Готово: движок сам сделает всё остальное.

## Управление

```powershell
python launcher.py                 # запустить bot_flat в фоне
python launcher.py bot_yrovni      # конкретного бота
python launcher.py bot_yrovni      # повторный вызов ОСТАНОВИТ его
```

## Бэктест

```powershell
python backtest.py --strategy flat --limit 500
python backtest.py --strategy yrovni --timeframe 5 --sl 0.2 --tp 0.4
python backtest.py --strategy trend --limit 1000
python backtest.py --strategy impulse --symbol XRPUSDT --timeframe 1
```

## Docker (24/7)

```powershell
docker compose up -d --build bot-flat    # один бот
docker compose up -d --build             # все пять
docker compose logs -f bot-trend
```

## Тесты и проверки кода

```powershell
uv pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\ruff.exe check .
.venv\Scripts\ruff.exe format .
.venv\Scripts\mypy.exe .
```

## Безопасность

- Начинайте всегда с `SIMULATION_MODE=true`.
- `TESTNET=false` — реальные деньги: проверьте стратегию бэктестом.
- Ключи лежат только в `.env` (в git не попадает).
