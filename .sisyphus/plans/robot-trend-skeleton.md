# План: robot_trend — обнуление + каркас технического слоя (без стратегии)

Статус: **черновик, заморожен пользователем** («запомни на этом месте, позже продолжим»).
Следующий шаг: доработать план → показать пользователю → получить approval → выполнять.

## Цель
Обнулить `robot_trend` и построить с нуля каркас «как торговать технически» без
стратегии. Места, требующие участия пользователя, помечать строкой
«**потом заполним**». Стратегия пишется отдельной задачей.

## Решения пользователя (через question tool)
1. **Объём каркаса — «Полный техслой»**: config + state + WS-фид + исполнение
   ордеров (лимит/маркет, серверный SL/TP, recover/ресинк) + гейты
   (день/кулдаун/фандинг/kill-файл) + main. Стратегия — заглушка, сделок нет.
2. **WS-фид — копия в robot_trend**: не трогаем живой `robot_zakol/data_feed.py`;
   дубль ~230 строк; при третьем потребителе вынести в `core/`.
3. **backtest.py — стаб `TrendStrategy`**: в `robot_trend/strategy.py` оставить
   класс-заглушку со старым интерфейсом (`name = "trend"`,
   `check_signal(candles) -> Signal` всегда hold, `_min_warmup`,
   `_max_lookback`, `on_position_closed`), чтобы корневой `backtest.py:35`
   не падал; `--strategy trend` даёт 0 сделок.

## Изученные факты (не переспрашивать)
- `robot_trend/` сейчас: `.env` (2 KB, старые стратегические параметры ATR/EMA/BB…),
  `main.py` (35 строк, старая архитектура `core.engine.Engine` + `TrendStrategy`),
  `strategy.py` (12.7 KB, legacy), `__init__.py`, `SESSION_NOTES.md` (1.5 KB);
  бэктеста внутри нет; git-статус по robot_trend пуст.
- Старым `core.engine` пользуются ещё 5 ботов (flat/impulse/krugloe/yrovni_D) —
  НЕ удалять, robot_trend переходит на новую архитектуру в стиле `robot_zakol`.
- `backtest.py:35,51` импортирует `TrendStrategy` (обратимо стабом).
- `app.py:1299` — только `page_bot("robot_trend")`, читает `.env` как текст
  (не импортирует код): после обнуления покажет дефолты/«-», это нормально.
- `tests/` — 0 упоминаний robot_trend; новых тестов не ломаем ничего.
- Шаблон архитектуры `robot_zakol/`: `.env`, `.env.example`, `config.py` (9.1 KB),
  `state.py` (5.2), `risk.py` (6.3), `data_feed.py` (9.2), `order_cycle.py` (39.2),
  `main.py` (5.8), `SESSION_NOTES.md`.
- Интерфейс старого `strategy.py` для стаба: `class TrendStrategy(BaseStrategy)`,
  `name = "trend"`, `check_signal(candles)`, `_min_warmup`, `_max_lookback`.
- `.env` robot_trend содержит API-ключи (сохранить те же ключи/TESTNET/SYMBOL).
- Стратегический блок текущего `.env` (ATR/EMA/BB/объём/сессии/выходы) —
  вычистить; старые значения остаются в git и в SESSION_NOTES (лучшие параметры
  там уже перечислены).
- Проверки: `uv run ruff check .`, `uv run ruff format .`,
  `uv run mypy robot_zakol robot_trend core`, `uv run pytest -q`.
  `mypy .` заблокирован старой синтаксической ошибкой
  `bot_screener_yrovni/scanner.py:137` — не трогать.

## Предлагаемая структура (к утверждению)
```
robot_trend/
  .env            # ТОЛЬКО технические параметры; блок стратегии — «# ПОТОМ ЗАПОЛНИМ»
  .env.example    # новый
  config.py       # pydantic/env-конфиг, технические поля + validate_for_live()
  state.py        # JSON-состояние: фаза/позиция/kill/день/счётчики/symbol+schema
  risk.py         # чистые хелперы гейтов: slippage/cooldown/daily/funding (без импортов на zakol)
  data_feed.py    # копия WS-фида из robot_zakol (reconnect/heartbeat)
  order_flow.py   # техническое исполнение: лимит/маркет, серверный SL/TP,
                  #   recover/ресинк, kill-switch (файл + equity), ORDER_LINK_ID
  strategy.py     # СТАБ TrendStrategy (hold + «потом заполним»)
  main.py         # точка входа в стиле robot_zakol: signal handlers, sync_time,
                  #   WS wait, recover, graceful shutdown
  SESSION_NOTES.md# дописать новую сессию
tests/test_robot_trend.py   # config-валидации, state roundtrip, гейты, стаб стратегии
```

## Что в «ПОТОМ ЗАПОЛНИМ»
- `.env`: блок стратегии (ATR/фильтры/сессии/выходы/размер позиции в %) —
  закомментированный шаблон с меткой.
- `strategy.py`: реальная логика входа/выхода.
- Точки интеграции в `order_flow.py`: хуки `on_signal(...)`/решение о сделке
  (техника готова, решение принимает стратегия).

## Шаги выполнения (после approval)
1. Обнулить: `__pycache__`, переписать `main.py`, `strategy.py` → стаб,
   `.env` → технический (ключи/TESTNET/SYMBOL/размер/WS/гейты/kill сохранить),
   добавить `.env.example`.
2. Написать `config.py`, `state.py`, `risk.py`, `data_feed.py` (копия),
   `order_flow.py`, `main.py`.
3. Тесты `tests/test_robot_trend.py` (по образцу `test_robot_zakol.py`).
4. Прогнать ruff check/format, mypy, pytest.
5. Дописать `robot_trend/SESSION_NOTES.md` (сессия + пометки «потом заполним»).
6. Сводка пользователю: что сделано, где стоят «потом заполним», как подключать
   стратегию.

## Открытые вопросы (уточнить при продолжении)
- Какой набор гейтов включить по умолчанию (как в zakol: COOLDOWN=15s,
  DAILY_*=0 выкл, FUNDING_AWARE=true)?
- Нужен ли trader-слойу сразу LLM/Telegram алерт или только логи?
- Имена файлов: `order_flow.py` vs `trader.py` — ок?
