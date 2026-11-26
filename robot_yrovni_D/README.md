# robot_yrovni_D — торговля POC-уровнями

Исполнитель сигналов POC-скринера. Скринер считает уровень (POC) и присылает
сигнал, робот выставляет вокруг него три лимитки, ведёт стоп, переносит его
в безубыток и закрывает сделку по тейку/трейлингу.

Стратегию уровней робот не считает — только потребляет сигналы.

## Как входит

Сигнал POC = 50, long (для short — зеркально):

| Что | Действие |
|---|---|
| 51 / 50 / 49 | три BUY-лимитки PostOnly, по 1/3 объёма (`ORDER_SPLIT`) |
| 45 (POC -10%) | стоп-маркет на весь объём |
| +1% от средней | стоп в безубыток, невыполненные лимитки отменяются |
| +7% | тейк-лимит на +7% и трейлинг 3% от пика |
| -7% от POC | тейк и трейлинг для short |
| цена ушла от POC > 15% | все лимитки отменяются |
| 24 часа | принудительное закрытие по рынку |
| после выхода | пауза `COOLDOWN_SEC` по символу |

Фильтры входа: `confidence >= MIN_CONFIDENCE`, `ATR(5m) <= MAX_ATR_PCT`
(0 — фильтр выключен), цена не дальше `MAX_DISTANCE_FROM_POC_PCT` от POC,
не в окне фандинга, notional одной лимитки >= минимума биржи.

## Запуск

```powershell
# DRY_RUN=true — PaperBook, реальные ордера не отправляются
.venv\Scripts\python.exe -m robot_yrovni_D.main

# проверка одного сигнала из файла (тоже учитывает DRY_RUN)
.venv\Scripts\python.exe -m robot_yrovni_D.main --once --signal-file signal.json
```

Сигналы приходят двумя путями (можно оба сразу):

- `POST http://127.0.0.1:8789/api/signal` (и `/api/poc-signal`) — скринер постит напрямую;
- поллинг дашборда `GET {DASHBOARD_URL}/api/poc-signals`, берутся только
  сигналы в состоянии `active`. `SIGNAL_POLL_SEC=0` отключает поллинг.

`GET /health` — проверка живости приёмника.

Формат `signal.json`:

```json
{
  "type": "POC_REACHED",
  "symbol": "BTCUSDT",
  "poc_price": 90000,
  "side_hint": "long",
  "confidence": 0.9,
  "current_price": 90050,
  "signal_id": "btc-90000-long-1"
}
```

Понимается и текущий формат скринера `POC_CLUSTER`
(`direction`, `entry_price`, `price`) — оба контракта сводятся к одному.

## Параметры

Все настройки — в `.env` (полный список с комментариями в `.env.example`).
Ключевые:

| Переменная | Дефолт | Смысл |
|---|---|---|
| `DRY_RUN` | `true` | `true` — PaperBook вместо биржи |
| `DEPOSIT_USD` | `100` | база расчёта объёма |
| `POSITION_PCT` | `6` | % депозита на сделку (делится на 3 лимитки) |
| `ORDER_SPLIT` | `0.33,0.34,0.33` | доли трёх лимиток (сумма 1.0) |
| `LEVEL_OFFSET_PCT` | `0.02` | отступ лимиток от POC |
| `STOP_INITIAL_PCT` | `0.10` | начальный стоп от POC |
| `BE_TRIGGER_PCT` | `0.01` | прибыль, при которой стоп идёт в BE |
| `TAKE_PCT` | `0.07` | тейк-лимит |
| `TRAIL_PCT` | `0.03` | трейлинг от пика |
| `MAX_HOLD_SEC` | `86400` | максимум жизни сделки |
| `MAX_CONCURRENT` | `1` | сколько сигналов вести одновременно |

Для волатильных альтов — переопределение по символу:

```
SYMBOL_OVERRIDES={"AKEUSDT": {"stop_initial_pct": 0.15, "level_offset_pct": 0.03}}
```

`MIN_NOTIONAL` биржи соблюдается автоматически: если объём одной лимитки
меньше минимума, сигнал пропускается с причиной `min_notional`
(поднимите `DEPOSIT_USD`/`POSITION_PCT`).

## Журнал

Пишется JSONL (`LOG_FILE`, по умолчанию `logs/strategy.jsonl`), в каждой
строке `ts` (UTC) и `dry_run`. Ротация по `LOG_ROTATION_MB`, хранение
`LOG_KEEP_DAYS` дней.

| Событие | Когда |
|---|---|
| `SIGNAL_RECEIVED` / `SIGNAL_SKIPPED` | сигнал принят / отклонён с `reason` |
| `LIMITS_PLACED` | выставлены 3 лимитки и стартовый стоп |
| `LIMIT_FILLED` | исполнена лимитка (с уровнями и объёмом) |
| `STOP_UPDATED` / `STOP_MOVED_BE` | пересчёт стопа от средней / перенос в BE |
| `LIMITS_CANCELLED` | отмена невыполненных лимиток с `reason` |
| `TAKE_ACTIVATED` | включены тейк и трейлинг |
| `TICK_SNAPSHOT` | срез позиции не чаще `TICK_SNAPSHOT_SEC` |
| `POSITION_CLOSED` | закрытие: `reason` = `stop`/`take`/`trail`/`be`/`max_hold` |
| `DAILY_SUMMARY` | итоги суток по сделкам |

Аварийная остановка: создать файл `KILL_SWITCH` (по умолчанию
`data/robot_yrovni_D.kill`) — новые сигналы перестанут приниматься.

## Проверки

```powershell
.venv\Scripts\python.exe -m pytest tests/test_robot_yrovni_d.py -q
.venv\Scripts\ruff.exe check robot_yrovni_D
.venv\Scripts\mypy.exe robot_yrovni_D
```
