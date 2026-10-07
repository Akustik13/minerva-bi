# Помічник по продукції (RAG) у Minerva

Окрема кнопка «Minerva» з аватаркою (поруч із 🏛 Minerva AI — це різні помічники, не об'єднані).
Відповідає за даташитами (векторна база RAG) і каталогом/складом Minerva.

## Схема

```
Браузер (адмінка Minerva) → /rag/api/… (Django, сесія користувача)
    → Minerva-сервер → RAG API v1 (Bearer-ключ) → пошук у даташитах / ранкер / LLM
```

Ключ RAG API зберігається лише в Minerva (`RagSettings.api_key`), у браузер не потрапляє.
Історія розмов — у Minerva (`RagConversation`, `RagMessage`); кожен користувач бачить лише свої розмови
(усі — лише суперадмін в «Розмови помічника»). RAG сам історію не зберігає: Minerva передає до 8 останніх
повідомлень розмови.

## Налаштування

AI та Боти / Minerva AI → «📚 Помічник по продукції» (`/admin/rag_assistant/ragsettings/`):
- **Адреса RAG API** — без `/v1`, напр. `https://app.sevskiy.com` або `http://192.168.2.68:8787`.
  Адреса має бути доступна **з сервера Minerva (NAS)**, не з браузера.
- **Ключ RAG API** — «Копіювати ключ API» у RAG Documents. Порожнє поле при збереженні — ключ не змінюється.
- «🔌 Перевірити зв'язок» — `GET /v1/health` + кількість документів.
- **Увімкнено** — показує кнопку всім співробітникам із доступом до модуля `rag_assistant`
  (адміни — завжди; іншим ролям модуль вмикається в профілі / пакеті).

## Протокол (RAG API v1)

- `POST /v1/ask` `{question, history, language, latest}` → `202 {job_id}`; `429` — RAG зайнятий (запит не прийнято,
  віджет повторює автоматично кожні 3 с).
- `GET /v1/jobs/{id}` → `running` (stage, trace) / `completed` (result: answer, sources, products, trace, usage) / `failed`.
- Minerva опитує завдання на кожен запит віджета (раз на 1,5 с), 404 → «завдання втрачено», >20 хв → тайм-аут.

## Ендпоінти Minerva (`/rag/…`, лише співробітники з доступом)

| Метод | Шлях | |
|---|---|---|
| GET | `/rag/api/state/` | увімкнено / налаштовано / розмови користувача |
| POST | `/rag/api/conversations/new/` | нова розмова |
| GET | `/rag/api/conversations/{id}/` | повідомлення |
| POST | `/rag/api/conversations/{id}/ask/` | `{question}` → 202 / 429 / 409 (є незавершене) |
| POST | `/rag/api/conversations/{id}/delete/` | видалити |
| GET | `/rag/api/messages/{id}/` | стан відповіді (опитує RAG) |

Товари з відповіді доповнюються даними Minerva: посилання на картку товару, доступний залишок, статус NRND/EOL.

## Тести

`python manage.py test rag_assistant --settings=tabele.settings_test` (RAG API підмінено mock-ом).
