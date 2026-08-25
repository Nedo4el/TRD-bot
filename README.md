# TRD Bot — набор торговых ботов для Bybit v5

Четыре независимых бота на общем движке. Общая логика (биржа, ордера,
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
├── bot_sma/               # SMA crossover (базовый, шаблон для новых)
├── bot_combo/             # Комбинированный фильтр: EMA+RSI+BB, M1
├── bot_adaptive/          # Адаптивный: SMA200 + EMA5/13, стопы от ATR
├── bot_swings/            # Свинг-уровни: пробой + ретест
├── backtest.py            # Бэктест любой стратегии
├── launcher.py            # Фоновый запуск/остановка бота
└── tests/                 # pytest-тесты стратегий и индикаторов
```

## Быстрый старт

```powershell
# 1. Зависимости
uv pip install -r requirements.txt

# 2. Настройки бота: скопировать пример и вписать ключи Bybit
Copy-Item bot_combo\.env.example bot_combo\.env
#    отредактировать BYBIT_API_KEY / BYBIT_API_SECRET

# 3. Запуск (безопасно: SIMULATION_MODE=true — без реальных ордеров)
python bot_combo/main.py
```

Каждый бот читает настройки ИЗ СВОЕЙ ПАПКИ (`bot_xxx/.env`).

## Боты и их параметры

Все «крутилки» вынесены в `.env` бота — код трогать не нужно.

| Бот | Идея | Вход | Выход |
|---|---|---|---|
| `bot_sma` | Пересечение SMA | золотое/мёртвое пересечение | SL/TP из .env |
| `bot_combo` | Тренд + выход RSI из зоны + фильтр BB | 3 условия одновременно | TP 0.5% / SL 0.3% |
| `bot_adaptive` | Направление по SMA200, вход по EMA5/13 | пересечение по тренду | SL=1.5×ATR, TP=2.5×ATR |
| `bot_swings` | Уровни по свингам (5 слева/справа) | пробой + ретест + свечной фильтр | TP 0.4% / SL 0.2% |

Сигналы считаются только по ЗАКРЫТЫМ свечам; позиция всегда одна.

## Как добавить свою стратегию

1. Скопируйте папку любого бота, например `bot_sma` → `bot_mya`.
2. В `bot_mya/strategy.py` напишите класс на базе `BaseStrategy`:
   метод `check_signal(candles) -> Signal` — единственное,
   что обязательно. Индикаторы берите из `core/indicators.py`.
3. В `bot_mya/.env` задайте свои числа.
4. Готово: движок сам сделает всё остальное.

## Управление

```powershell
python launcher.py                 # запустить bot_sma в фоне
python launcher.py bot_combo       # конкретного бота
python launcher.py bot_combo       # повторный вызов ОСТАНОВИТ его
```

## Бэктест

```powershell
python backtest.py --strategy sma --limit 500
python backtest.py --strategy combo --timeframe 1 --sl 0.3 --tp 0.5
python backtest.py --strategy adaptive --limit 1000
python backtest.py --strategy swings --symbol XRPUSDT --timeframe 5
```

## Docker (24/7)

```powershell
docker compose up -d --build bot-combo   # один бот
docker compose up -d --build             # все четыре
docker compose logs -f bot-adaptive
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
