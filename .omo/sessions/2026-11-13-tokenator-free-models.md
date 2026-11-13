# Сессия 2026-11-13 — бесплатные модели Tokenator в opencode

Время: 13.11.2026, ~21:25 МСК.

## Запрос пользователя
Подключить бесплатные модели Tokenator
(`free-glm-5.3-flash`, `free-gpt-6-astra`, `free-claude-opus-5.5`,
`free-minimax-m3.1-flash`, `free-gemini-4-carbon`) к opencode, чтобы они
были доступны для выбора. Вопрос: «что тебе для этого нужно?» — ответ:
ничего, ключ уже был в конфиге.

## Что сделано
Правка только в глобальном конфиге
`C:\Users\79095\.config\opencode\opencode.json`, в существующем провайдере
`Tokenator` (`@ai-sdk/openai-compatible`, `https://api.tokenator.cloud/v1`).
Добавлены 5 моделей, у каждой `cost {input:0, output:0}`:

| ID                        | context | output | особенности                                  |
|---------------------------|---------|--------|----------------------------------------------|
| `free-claude-opus-5.5`    | 1000000 | 128000 | text/image/pdf, variants high/medium/low     |
| `free-gpt-6-astra`        | 1000000 | 128000 | text/image                                   |
| `free-glm-5.3-flash`      | 1000000 |  65536 | text/image                                   |
| `free-minimax-m3.1-flash` | 1000000 |  65536 | text/image, variants high/medium/low         |
| `free-gemini-4-carbon`    | 1050000 | 128000 | text/image/audio/video/pdf, variants          |

Проектный `D:\Vcode\TRD bot\opencode.jsonc` не трогали (там только `$schema`).

## Проверки (все прошли)
- `GET /v1/models` ключом из конфига: все 5 ID присутствуют.
- Прямой `POST /v1/chat/completions` каждым из 5 ID → ответ `OK`.
- JSON конфига валиден; `opencode models` показывает `Tokenator/free-*`.
- `opencode run -m Tokenator/free-glm-5.3-flash "Reply with exactly: PONG"`
  → `PONG` (модель работает через opencode, не только через API).

## Суточные квоты бесплатных моделей
GLM 5.3 Flash — 3.5M токенов / 60 запросов; GPT-6 Astra — 4M / 45;
Claude Opus 5.5 — 3M / 20; MiniMax M3.1 Flash — 3M / 50;
Gemini 4 Carbon — 4M / 30. Обновляются раз в сутки, пакетные токены
не расходуются.

## Замечания
- Нужен перезапуск opencode: конфиг читается только при старте.
- В конфиге `"model": "Tokenator/claude-opus-5.5"`, но такой модели в
  провайдере `Tokenator` нет — она в `Tokenator-Anthropic`. Дефолт может
  не подхватиться; не исправляли (вне рамок запроса).
- API-ключ Tokenator лежит в конфиге открытым текстом и засветился в
  чате этой сессии при проверке `curl` — стоит перевыпустить в кабинете.
- `tokenator.top` и `api.tokenator.cloud` отдают одинаковый список моделей;
  используется `api.tokenator.cloud` (как было до сессии).