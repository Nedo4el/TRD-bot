# Сессия 2026-11-08 (продолжение) — top-tools-ai + коммит скринера «Все монеты»

Время: 08.11.2026, ~11:38 МСК.

## 1. top-tools-ai (opencode provider)

### Диагноз
- Ключ и API рабочие: `GET /api/v1/models` → 200, 22 модели; chat, streaming, tool_calls — всё отвечает.
- ID моделей в `opencode.jsonc` совпадают со списком провайдера.
- Реальная причина сбоя — в `%USERPROFILE%\.local\share\opencode\log\opencode.log`:
  `AI_APICallError: Not enough one-time welcome tokens remain for this request. Shorten the conversation or subscribe.`
  Возникало на `agent=compaction` (большой контекст).

### Тарифы провайдера
- PAYG (`billing_mode=payg` в `/models`): `DeepSeek-V4-Flash-Vision-Exp`, `gpt-5.6-sol`, `claude-opus-5`.
- Приветственный баланс 10M токенов — единоразовый, общий для не-PAYG моделей (кроме `Top-Tools-Ai`); не пополняется.
- `Top-Tools-Ai` — отдельный **дневной** лимит 10M (сбрасывается каждый день).

### Решение (применено)
- `C:\Users\79095\.config\opencode\opencode.jsonc`:
  - `model` и `small_model` → `top-tools-ai/Top-Tools-Ai`
  - `limit.context` у `Top-Tools-Ai` исправлен `512000 → 400000` (реальный лимит модели).
- `Top-Tools-Ai` проверен вживую: обычный ответ, streaming, tool_calls — OK.
- Нужен **перезапуск opencode** (конфиг читается при старте).

### Замечания
- Сеть до `top-tools-ai.com` с этой машины рвётся на передачах >~8–30 КБ
  (их JS 660 КБ обрывается на 8 КБ; chat-запросы >~30 КБ — reset). Похоже на MTU/VPN/ISP,
  а не на провайдера. При повторных `socket connection was closed unexpectedly` — проверить VPN/MTU.
- API-ключ засветился в чате → рекомендуется перевыпустить в дашборде.

## 2. Дашборд — скринер «Все монеты» (закоммичено)

### Код
- `dashboard/settings.py` (новый): `AllCoinsSettings` (enabled, interval_sec, timeframe,
  atr_period=14, min_turnover_24h, max_symbols, exclude_symbols, min_abs_change_24h,
  volume_trend, sort_mode), `load()`, `save()` (атомарно), `from_dict()`, `to_dict()`;
  JSON в `data/dashboard/settings.json`.
- `dashboard/config.py`: `ScreenerSpec("all", "Все монеты", "all", 60, 100)` в `SCREENERS`.
- `dashboard/adapters.py`: `scan_all_coins()`, `_all_coins_sort_key()`, `ADAPTERS["all"]`;
  ATR% через `core.indicators.atr`, тренд объёма по 2 последним закрытым свечам;
  фильтры по обороту / |Δ24ч| / exclude; top-N по обороту; `enrich_all()` пачками по 10.
- `dashboard/server.py`: `GET/POST /api/settings/all` (`api_settings_all_get/post`).
- `dashboard/static/index.html`: панель настроек (toggle), 10 колонок
  (Δ24ч %, ATR %, Объём ↑/↓, Оборот 24ч), сортировка, хелперы форматирования.
- `tests/test_dashboard.py`: тесты настроек, `scan_all_coins`, API-эндпоинтов
  (прямые вызовы хендлеров, т.к. `TestClient` недоступен без httpx2).

### Проверки
- `uv run pytest tests/test_dashboard.py -q` → 22 passed.
- `uv run ruff check dashboard/ tests/test_dashboard.py run_dashboard.py` → OK.
- `uv run ruff format` → 4 файла отформатированы.
- `uv run mypy dashboard/ tests/test_dashboard.py run_dashboard.py` → OK.
- Коммит `b50d587`: `dashboard: screener «Vse monety» - ATR%, volume trend, settings panel`
  (6 файлов, +729/−22).

## Состояние проекта
- Последний коммит: `b50d587`.
- Рабочее дерево: чистое (кроме этого session-лога).
- Следующий шаг: перезапустить opencode (подхватить `Top-Tools-Ai`).

## Полезные команды

```powershell
uv run pytest tests/test_dashboard.py -q
uv run ruff check dashboard/ tests/test_dashboard.py run_dashboard.py
uv run mypy dashboard/ tests/test_dashboard.py run_dashboard.py
```
