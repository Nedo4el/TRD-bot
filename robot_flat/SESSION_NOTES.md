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
- [x] Внедрить стратегию flat: `decide()`/`check_signal()` + параметры в
      `.env`/`.env.example` + тесты стратегии
- [ ] Live-прогон скелета (recover + WS + kill-switch) на BRUSDT

---

## Сессия 2026-10-07: стратегия flat v1 (EMA/ADX/ATR + Volume)

**Указание:** must-have EMA 20/50/200 + ADX 14 (порог 25) + ATR 14; бэктест
4H за год (2025-10-07 → 2026-10-07); PF > 1.2 → по очереди RSI и Volume,
оставлять только при реальном улучшении PF; упал PF → выкинуть.

**Реализация:** `strategy.py` state-based (не кроссовый — кроссы давали только
шум при ADX≈0): состояние long = EMA20>EMA50 AND close>EMA200, требование
ADX ≥ 25; стоп/тейк = ATR×1.5/×3; слой риска (sl/be/trail) выключен —
гейт в `risk.on_price` отдаёт стоп нетронутым. Фильтры RSI/Volume —
env-флаги `RSI_FILTER`/`VOLUME_FILTER` (контракт `FlatStrategy()` без
аргументов сохранён). Тесты: 65 в `tests/test_robot_flat.py` (всего 299).

**v1 — только must-have (7 символов):**

| Символ | Сделки | Win% | PF | PnL% | DD% |
|---|---|---|---|---|---|
| KAVAUSDT | 75 | 45.3 | **1.80** | +116.40 | 15.06 |
| ARKUSDT | 73 | 39.7 | **1.48** | +99.61 | 44.12 |
| BANDUSDT | 97 | 34.0 | 1.17 | +34.56 | 35.09 |
| BTCUSDT | 88 | 29.5 | 1.07 | +8.13 | 21.45 |
| ADAUSDT | 80 | 30.0 | 0.88 | −23.92 | 38.26 |
| BRUSDT | 109 | 24.8 | 0.81 | −115.13 | 144.06 |
| LSKUSDT | 88 | 28.4 | 0.78 | −67.78 | 74.71 |

ΣPnL +51.9%, PF средний 1.141, медиана 1.07. Порог PF>1.2 выполнен →
тестируем фильтры.

**v2 = v1 + RSI_FILTER=true → ОТКЛОНЁН:**

- PF средний 1.141→**1.070**, медиана 1.07→1.05, ΣPnL +51.9→**−35.1**;
- главные прибыльные символы просядут: ARK 1.48→1.28, KAVA 1.80→1.55,
  BAND 1.17→1.05.
- По правилу «PF упал — выкидываем» → `RSI_FILTER=false` (зафиксировано в `.env`).

**v3 = v1 + VOLUME_FILTER=true (VOL_MULT=1.3×SMA20) → ОСТАВЛЕН:**

- PF по символам: BTC 1.07→1.17, LSK 0.78→0.84, ARK 1.48→1.66, BAND
  1.17→1.46 (4/7 ↑); медиана 1.07→**1.17**, сумма PF 7.99→8.02 (не упала);
- контрольный BTC улучшился и по PnL (+8.1→+14.1), и по DD (21.5→17.2);
- минусы: BRU 0.81→0.62 (символ и так неприбылен), KAVA 1.80→1.46.

**Итог:** `.env`/`.env.example` — EMA 20/50/200, ADX 14/25, ATR 14 ×1.5/×3,
`RSI_FILTER=false`, `VOLUME_FILTER=true`. Проверки: pytest 299 passed,
ruff check/format и mypy по своим файлам — чисто; `FlatParams.from_env()`
из `.env` даёт use_volume=True, use_rsi=False.
