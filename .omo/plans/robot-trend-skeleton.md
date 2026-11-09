# План: robot_trend — обнуление + каркас технического слоя (без стратегии)

Статус: **готов к утверждению** (3 развилки решены пользователем, детали дособраны).

## Цель
Обнулить `robot_trend` и построить с нуля каркас «как торговать технически» без
стратегии. Все места, требующие участия пользователя, помечать строкой
«**потом заполним**». Логика входа/выхода (стратегия) — отдельная задача.

## Решения пользователя (через question tool — не переспрашивать)
1. **«Полный техслой»**: config + state + risk-гейты + WS-фид + исполнение
   ордеров (лимит/маркет, серверный SL/TP, recover/ресинк) + kill-switch + main.
2. **Копия `data_feed.py` в robot_trend** — живой `robot_zakol` не трогаем
   (дубль ~230 строк; при третьем потребителе вынести в `core/`).
3. **Стаб `TrendStrategy`** в `strategy.py` — корневой `backtest.py:35` не
   падает, `--strategy trend` даёт 0 сделок (hold).

## Изученные факты (не переспрашивать)
- Текущий `robot_trend/`: `.env` (2 KB со старыми стратегическими параметрами
  ATR/EMA/BB/объём/сессии + API-ключи), `main.py` (35 строк, старая архитектура
  `core.engine.Engine` + `TrendStrategy`), `strategy.py` (12.7 KB legacy),
  `__init__.py`, `SESSION_NOTES.md` (1.5 KB). Git по robot_trend чистый.
- `core.engine` используют ещё 5 ботов (flat/impulse/krugloe/yrovni_D) — НЕ
  трогать; robot_trend переходит на новую архитектуру в стиле `robot_zakol`.
- `backtest.py:35,51` → `TrendStrategy` (обратимо стабом); `app.py:1299` только
  читает `.env` как текст (импортов кода нет).
- `tests/` — 0 упоминаний robot_trend.
- Интерфейс стаба из legacy `strategy.py`: `class TrendStrategy(BaseStrategy)`,
  `name = "trend"`, `check_signal(candles) -> Signal` (всегда hold),
  `_min_warmup`, `_max_lookback`, `on_position_closed`.
- Шаблон `robot_zakol/`: config.py (pydantic, default_factory на env,
  `validate_for_live()`), state.py (JSON, `check_symbol`), risk.py (чистые
  хелперы), data_feed.py (public ticker + private order/execution/wallet/
  position, reconnect/heartbeat), main.py (signal handlers, `sync_time`,
  hedge-проверка, WS wait, recover, graceful shutdown).
- Тесты-образцы: `tests/test_robot_zakol.py` (`_cfg()`, `FakeClient`,
  `FakeFeed`, `_make_cycle`, `tmp_path` для state).
- Канделы: REST `client.get_klines` есть; WS kline-потока в проекте нет —
  в копию фида добавить `ws.kline_stream(...)` (pybit unified) + REST-бутстрап.
- Проверки: `uv run ruff check .`, `uv run ruff format .`,
  `uv run mypy robot_zakol robot_trend core`, `uv run pytest -q`.
  `mypy .` заблокирован старой ошибкой `bot_screener_yrovni/scanner.py:137` —
  не трогать.

## Структура (что делаем)
```
robot_trend/
  .env            # переписать: ТОЛЬКО техника; стратегический блок = «# ПОТОМ ЗАПОЛНИМ»
  .env.example    # новый (без секретов)
  config.py       # TrendConfig (pydantic, env) + validate_for_live()
  state.py        # TrendState/StateStore: позиция/ордеры/день/kill/symbol+schema
  risk.py         # чистые гейтеры: slippage_ok/cooldown_remaining/daily_stop_reason
                  #   (копия хелперов, БЕЗ импортов на robot_zakol)
  data_feed.py    # копия из robot_zakol + подписка kline.{TF}.{symbol} в public WS
  order_flow.py   # ТЕХНИКА исполнения:
                  #   enter(side, qty, price?, stop?, take?) — лимит/маркет + серверный SL/TP
                  #   exit() — market reduceOnly
                  #   recover() — ресинк state↔биржа, отмена сирот
                  #   kill-switch: файл + equity-драйдроу (should_kill)
                  #   гейты входа: день UTC → daily → cooldown → funding → slippage
                  #   цикл: ждём решение СТРАТЕГИИ → исполняем; стратегия молчит → hold
  strategy.py     # СТАБ: TrendStrategy для backtest.py (hold) +
                  #   новый хук decide(...) -> Instruction | None  «ПОТОМ ЗАПОЛНИМ»
  main.py         # вход в стиле robot_zakol: load_config → validate_for_live →
                  #   sync_time → filters/hedge-check → StateStore → recover →
                  #   WS wait → цикл → graceful shutdown
  SESSION_NOTES.md# дописать сессию: что за каркас, где «потом заполним»
tests/test_robot_trend.py   # config-валидации, state roundtrip, гейты,
                            #   стаб-стратегия, order_flow на FakeClient
```

### `.env` каркаса (технический блок)
- Сохранить как есть: `BYBIT_API_KEY/SECRET`, `TESTNET`, `SYMBOL`, `TIMEFRAME=5`
- Взять из zakol-набора: `CATEGORY`, `DEPOSIT_USD`, `QTY`, `POSITION_PCT`,
  `REQUESTS_PER_SECOND`, `WS_ENABLED`, `LOG_LEVEL`, `LOG_FILE`, `STATE_FILE`,
  `SLIPPAGE_PCT=0.005`, `COOLDOWN_SEC=15`, `DAILY_LOSS_LIMIT=0`,
  `MAX_TRADES_PER_DAY=0`, `RECONNECT_SEC=2`, `HEARTBEAT_SEC=1`,
  `RECV_WINDOW=10000`, `TIME_SYNC=true`, `KILL_SWITCH=data/trend.kill`,
  `ORDER_LINK_ID_PREFIX=tr-` (1..17 символов), `FUNDING_AWARE=true`,
  `FUNDING_WINDOW_SEC=60`
- Удалить (старые стратегические): ATR/EMA/ADX/BB/Keltner/объём/сессии/выходы/
  SCAN/прочее. В конце `.env` — закомментированный блок
  `# === СТРАТЕГИЯ — ПОТОМ ЗАПОЛНИМ ===` (пустой шаблон).

### Точки «ПОТОМ ЗАПОЛНИМ» (явно)
1. `strategy.py` — реальная логика (`decide()` всегда `None`/hold + TODO).
2. `.env` — стратегические параметры (закомментированный шаблон).
3. `order_flow.py` — вызов `strategy.decide(...)` помечен комментарием; вся
   механика вокруг уже работает и тестируется без него.

## Шаги выполнения
1. Обнулить: удалить `__pycache__`, `strategy.py` → стаб, `main.py` → новый,
   `.env` → технический, добавить `.env.example`.
2. `config.py` (поля + валидации как в zakol: пустой SYMBOL, CATEGORY≠linear,
   WS≠true, QTY/DEPOSIT≤0, POSITION_PCT∉(0,100], RECV_WINDOW∉(0,50000],
   ORDER_LINK_ID_PREFIX длина 1..17, RECONNECT/HEARTBEAT≤0, SLIPPAGE∉[0,1),
   отрицательные daily/cooldown → ValueError).
3. `state.py` (SCHEMA_VERSION, PendingOrder/OpenPosition, day/day_pnl/day_trades,
   last_close_at, kill-флаг).
4. `risk.py` — скопировать чистые хелперы гейтов.
5. `data_feed.py` — копия + kline-подписка (+ REST-бутстрап свечей в main).
6. `order_flow.py` — enter/exit/recover/kill/гейты/цикл со стратегическим хуком.
7. `main.py` — точка входа.
8. `tests/test_robot_trend.py` — по образцу zakol-тестов.
9. Проверки: `uv run ruff check .` → `uv run ruff format .` →
   `uv run mypy robot_zakol robot_trend core` → `uv run pytest -q`.
10. Дописать `robot_trend/SESSION_NOTES.md`; сводка пользователю
    (что готово, где «потом заполним», как подключать стратегию).

## Критерии готовности
- ruff/mypy/pytest зелёные (154+ старых теста не сломаны).
- `python backtest.py --strategy trend` не падает (0 сделок).
- Каркас стартует в SIMULATION/TESTNET без стратегии: цикл живёт, входов нет,
  kill-switch/гейты/recover работают и покрыты тестами.

## Открытые вопросы
- Нет. Детали решены: дефолты гейтов — как в zakol; оповещения — через
  существующий `core.notifier.Notifier`; имя файла — `order_flow.py`.

---

# ДОПОЛНЕНИЕ (запомнить): контракт стратегии + план коммита

## Контракт `decide()` — единственная точка подключения стратегии
Объяснено пользователю 2026-09-29, подтверждено («ок, пометь и запомни»):

```python
# robot_trend/strategy.py
def decide(self, candles: list[Candle], position: OpenPosition | None
           ) -> Instruction | None:
```

- `None` — hold, ничего не делать (стаб возвращает всегда None → сделок нет);
- `Instruction(action="enter", side="long"|"short", stop=..., take=..., reason=...)`
  — техника: гейты → объём (POSITION_PCT/QTY) → Market → серверный SL/TP;
  **stop обязателен** (без стопа вход отклоняется);
- `Instruction(action="exit", reason=...)` — техника: Market-выход + счётчики
  (день/сессия/кулдаун) + state.

Вызов: `order_flow._on_candles()` — каждая новая ЗАКРЫТАЯ свеча (REST, дедуп
по open_time; WS kline лишь триггер). Всё остальное (гейты kill→день→
cooldown→фандинг→слippаж, recover, kill-switch, state) уже готово и тестами
покрыто — писать стратегию = менять только `decide()` + её параметры в
блоке `.env` `# СТРАТЕГИЯ — ПОТОМ ЗАПОЛНИМ`.

## Статус сборки (2026-09-29, выполнено)
- ruff check/format чисто; `mypy robot_trend robot_zakol core` — 0 ошибок;
  `pytest` — **202 passed** (+48 в `tests/test_robot_trend.py`);
  `backtest.py --strategy trend` — 0 сделок, не падает; `validate_for_live()` OK.

## План коммита (СЛЕДУЮЩИЙ ШАГ, исполнить после выхода из plan mode)
`robot_trend/.env` НЕ коммитить — `.gitignore:2 **/.env` (секреты).

Стейджим (git add):
- изменённые: `robot_trend/main.py`, `robot_trend/strategy.py`,
  `robot_trend/SESSION_NOTES.md`, `.sisyphus/plans/robot-trend-skeleton.md`
- новые: `robot_trend/config.py`, `robot_trend/state.py`, `robot_trend/risk.py`,
  `robot_trend/data_feed.py`, `robot_trend/order_flow.py`,
  `robot_trend/.env.example`, `tests/test_robot_trend.py`

Сообщение (стиль репо, короткие русские темы):
`robot_trend: каркас технического слоя без стратегии (+48 тестов)`

Не коммитить: ничего лишнего; авто-синк-коммиты не трогать; `git add` только
перечисленное (не `git add -A`, т.к. рядом есть чужие незакоммиченные файлы
bot_screener_* — проверить `git status` перед стейджем).
