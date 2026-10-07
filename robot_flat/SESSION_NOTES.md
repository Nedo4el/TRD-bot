# Robot Flat — Состояние на 2026-10-07

- **Технический скелет внедрён** (как у robot_trend / robot_zakol, план
  `.sisyphus/plans/robot-trend-skeleton.md`): config (pydantic) + state (JSON,
  атомарно) + risk (гейты: kill → день UTC → cooldown → фандинг → слippаж,
  BE/trail) + data_feed (WS ticker/kline/order/execution/wallet/position) +
  order_flow (recover → цикл по закрытым свечам → Market + серверный SL/TP) +
  main (SIGINT/SIGTERM, graceful shutdown).
- **Стратегия — стаб**: `decide()` всегда None, `check_signal()` всегда hold.
  Входов нет: live не торгует, `backtest.py --strategy flat` → 0 сделок.
  Стратегия внедряется отдельным шагом.
- **Один ТФ** (в отличие от trend): HTF-полей в конфиге нет, `decide(candles,
  position)`.
- Наследие POC-стратегии (старый `strategy.py`/`main.py` на engine) сохранено
  в git и в закомментированном блоке `.env` — параметры для будущего внедрения.

---

# Robot Flat — Сессия 2026-10-07 — СКЕЛЕТ ТЕХСЛОЯ

## Изменения по файлам
| Файл | Изменение |
|------|-----------|
| `robot_flat/config.py` | новый: `FlatConfig` (pydantic) — техполя из `robot_trend/config.py` **без HTF**; kill-файл `data/flat.kill`, префикс `fl-`, логи `logs/flat.log`, state `data/flat_state.json` |
| `robot_flat/state.py` | новый: `FlatState`/`OpenPosition`/`StateStore` (SCHEMA_VERSION=1, symbol-check) |
| `robot_flat/risk.py` | новый: чистые функции `on_price` (BE+трейлинг), `entry_qty`, `slippage_ok`, `cooldown_remaining`, `daily_stop_reason`, `should_kill`, `position_pnl` |
| `robot_flat/data_feed.py` | новый: копия trend-фида (kline confirm → очередь, resync после WS-реконнекта); имена потоков `flat-ws-*` |
| `robot_flat/order_flow.py` | новый: исполнительный цикл без HTF: гейты входа, Market + серверный SL/TP, `_update_risk` (BE/trail через `strategy.params`), recover/shutdown; `decide(closed, position)` |
| `robot_flat/main.py` | переписан: точка входа по образцу trend (recover, WS-wait, сигналы); сохранён `sys.path`-insert (работает `python robot_flat/main.py` локально и в Docker CMD) |
| `robot_flat/strategy.py` | заменён стабом: `Instruction`, `FlatParams` (sl/be/trail, `from_env`), `FlatStrategy` = hold; старый POC-код — в git history |
| `robot_flat/.env` | переписан: техблок (ключи/BRUSDT/TIMEFRAME=5 сохранены, `fl-` префикс); старые POC-параметры — в закомментированном блоке «СТРАТЕГИЯ — ПОТОМ ЗАПОЛНИМ»; удалены legacy `SIMULATION_MODE`/`POLL_INTERVAL`/`KLINE_LIMIT` (новый цикл их не читает) |
| `robot_flat/.env.example` | новый: шаблон без секретов |
| `tests/test_robot_flat.py` | переписан: +60 тестов (config/state/risk/стаб/порядок гейтов/order_flow/main) вместо legacy POC-тестов |

## Проверки
- `pytest` — **294 passed**; `ruff check robot_flat tests` + `ruff format --check` —
  чисто; `mypy robot_flat tests/test_robot_flat.py` — 0 ошибок.
- `backtest.py --strategy flat --no-chart` — стартует, 0 сделок (hold), без падений.
- Совместимость: `backtest.py:32` и `diag_ena.py:10` импортируют
  `FlatStrategy()` без аргументов — стаб совместим; `app.py`/docker-compose
  читают только `.env`.

## Решения / компромиссы
- **HTF убран из конфига** — flat одно-ТФ; если стратегии понадобится старший
  ТФ, поля добавятся заново (как в trend).
- **`FlatParams` оставил 4 поля риска** (sl/be/trail), которые читает
  `_update_risk`: order_flow не менялся относительно trend-шаблона;
  будущая стратегия дополнит dataclass своими полями.
- **`SIMULATION_MODE` удалён**: новый цикл его не использует (legacy
  читал только `core/engine.py`); до внедрения стратегии бот всё равно
  не входит в рынок (hold).
- `ruff check .` по всему репо: 20 ошибок в `bot_screener_impulse/`
  (legacy, не трогали — вне зоны задачи).

## TODO
- [ ] Внедрить стратегию flat: `decide()`/`check_signal()` + параметры в
      «СТРАТЕГИЯ — ПОТОМ ЗАПОЛНИМ» + тесты стратегии
- [ ] Live-прогон скелета (recover + WS + kill-switch) на BRUSDT
