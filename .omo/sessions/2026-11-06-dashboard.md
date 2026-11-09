# Сессия 2026-11-06 — дашборд скринеров + провайдер opencode

Время: 06.11.2026, ~21:20 МСК.

## Что делали

### 1. Провайдер Top Tools AI в opencode
- Файл: `C:\Users\79095\.config\opencode\opencode.jsonc`
- Провайдер `top-tools-ai` (`@ai-sdk/openai-compatible`),
  base URL `https://top-tools-ai.com/api/v1`, 19 моделей.
- Дефолт: `top-tools-ai/deepseek-v4.1-flash-1017`.
- Проверено: `opencode models` видит провайдера, API отвечает.
- Ключ лежит в конфиге открытым текстом; в чате он засветился —
  желательно перевыпустить в дашборде Top Tools AI.
- Требуется рестарт opencode после правки конфига.

### 2. Дашборд `http://127.0.0.1:8787/`
Запущен: PID 3908, `run_dashboard.py`, старт 20:03 МСК.
Все 8 скринеров отработали, ошибок 0, ~107 сигналов.

## Состояние проекта

Последний коммит: `454fed0 auto-sync 2026-10-31 10:05`.
Всё сделанное по дашборду **не закоммичено**:

- `dashboard/` — Starlette + uvicorn: `server.py`, `scheduler.py`,
  `adapters.py`, `store.py`, `models.py`, `tiger.py`,
  `static/index.html`.
- `run_dashboard.py`, `dashboard.cmd`.
- `tests/test_dashboard.py` — 16 тестов, проходят.
- `requirements.txt` — добавлены `starlette`, `uvicorn`.
- Мелочи: типизация `bot_screener_klin/scanner.py`,
  импорты в `bot_screener_yrovni/scanner.py`.

## Что выяснили

- Утром/днём 06.11 Bybit отдавал 403 `blocked from your country`
  (CloudFront) и при прямом запросе, и через xray (выход Амстердам).
- Вечером доступ восстановился: `api.bybit.com/v5/market/time` → 200.
- Реальная причина вечерних 403 — **rate-limit Bybit от стартового
  залпа**: 8 скринеров одновременно дёргают тикеры → временный бан IP,
  отпускает через несколько минут.
- `DASH_CONCURRENCY=1` + тяжёлые сканы → очереди по 1–3 мин,
  интервалы «плывут» (считаются после завершения, а не по расписанию).

## Следующие шаги (обсуждали, не делали)

1. Джиттер на старте — разносить запуск скринеров.
2. Ретрай с backoff на 403/429 в fetcher.
3. Запуск по абсолютному расписанию, а не «after completion».
4. Опционально: поддержка `HTTPS_PROXY`/socks в fetcher.
5. Закоммитить дашборд.
6. Починить кодировку логов (кракозябры в `logs/dashboard.log`).

## Полезные команды

```powershell
# запуск дашборда
.\.venv\Scripts\python.exe run_dashboard.py
# или dashboard.cmd

# тесты
uv run pytest tests/test_dashboard.py -q

# проверка API
Invoke-RestMethod http://127.0.0.1:8787/api/signals
```