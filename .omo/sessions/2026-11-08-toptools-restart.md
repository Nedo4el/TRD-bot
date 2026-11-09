# Сессия 2026-11-08 (продолжение 2) — починка провайдера top-tools-ai

Время: 08.11.2026, ~12:25 МСК.

## Что было сломано
Провайдер `top-tools-ai` **переименовал модели**. Старые ID из
`opencode.jsonc` (`Top-Tools-Ai`, `DeepSeek-V4-*`, `GLM-*`, `Qwen-*`,
`Kimi-*`, `MiniMax-*`, `Step-5-*`, `MiMo-*`) больше не работают.

Ошибка из `%USERPROFILE%\.local\share\opencode\log\opencode.log`:

```
We've just updated our models! Old model names no longer work.
Use "Top-Tools-Ai-Basic", "Top-Tools-Ai-Pro" or "Top-Tools-Ai-Max",
and see the plans at https://top-tools-ai.com/#plans
```

## Актуальное состояние провайдера (проверено ключом)
`GET https://top-tools-ai.com/api/v1/models` теперь отдаёт **только 3 модели**:

| ID                    | context | статус для нашего ключа              |
|-----------------------|---------|--------------------------------------|
| `Top-Tools-Ai-Basic`  | 512000  | **OK** (работает на welcome 10M)      |
| `Top-Tools-Ai-Pro`    | 1000000 | 402 «требуется оплата»                |
| `Top-Tools-Ai-Max`    | 1000000 | 402 «требуется оплата»                |

`chat/completions` на `Top-Tools-Ai-Basic` → `pong`, usage 8 токенов.
То есть 10M приветственных токенов тратятся именно через **Basic**.

## Что сделано
- `C:\Users\79095\.config\opencode\opencode.jsonc` переписан:
  - `model` и `small_model` → `top-tools-ai/Top-Tools-Ai-Basic`.
  - В `models` оставлены 3 новые модели (Basic/Pro/Max), старые удалены.
  - `limit.context`: Basic 512000, Pro/Max 1000000, output 32768.
- Проверки: JSON валиден; `opencode models` показывает
  `top-tools-ai/Top-Tools-Ai-Basic`, `-Pro`, `-Max`.
- Требуется перезапуск opencode (конфиг читается при старте).
  Запуск: `opencode --continue` (продолжить последнюю сессию).

## Замечания
- Ключ снова засветился в чате → перевыпустить в дашборде top-tools-ai.
- Сеть до `top-tools-ai.com` с этой машины рвётся на крупных передачах
  (>~8–30 КБ) — вероятно MTU/VPN/ISP, не провайдер.
