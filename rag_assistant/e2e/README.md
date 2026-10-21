# Наскрізний тест Minerva ↔ приватний RAG (dev, синтетичні дані)

Усе на одному ПК і лише через loopback: робочі БД, пошта, Synology та M7 не використовуються.

| Що | Де |
|---|---|
| Dev-Minerva (гілка `feat/knowledge-feed`) | `http://127.0.0.1:8010` |
| `source_instance_id` | `minerva-dev-e2e` |
| PostgreSQL | контейнер `minerva-e2e-pg`, `127.0.0.1:55433`, БД `minerva_e2e` |
| Media, секрети | `%USERPROFILE%\minerva-e2e\` (`secrets\` — лише поточний користувач і SYSTEM) |
| Токен Minerva для RAG (`knowledge:read` + `knowledge:write`) | `secrets\minerva-knowledge-token.txt` |
| Приватний RAG (очікується) | `http://127.0.0.1:8788`, колекція `crm_correspondence_test` |

Налаштування — `tabele/settings_e2e.py`: окрема БД (без `E2E_DB_PASSWORD` не завантажуються), схема з моделей,
власні cookie, Django-пошта не надсилається, без автобекапів. Скриньки мають хости `*.invalid` — IMAP/SMTP недоступні.

## Запуск

```powershell
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\start_postgres.ps1
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\start_minerva.ps1 -Background   # окремий процес, журнал у logs\
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\e2e.ps1 seed           # ідемпотентно
```

`seed` створює:
- `e2e_anna` (власниця `vertrieb@minerva-e2e.test`) і `e2e_boris` (`service@minerva-e2e.test`), без паролів;
- клієнта, постачальника, проєкт `P-2026-001`;
- сім листів:
  - m1–m3 — гілка ANT-2400 з обіцянкою KW 46 і TXT-вкладенням;
  - m4 — постачальник, CAB-7, дата 14.11.2026;
  - m5 — внутрішній, знижка 8 %;
  - m6 — розсилка (не індексується);
  - m7 — у скриньці Бориса, Kalibrierprotokoll SN-77821.

Також `seed` записує токен у файл; значення ніде не друкується.

Токен для RAG у змінну середовища, не показуючи його:

```powershell
$env:MINERVA_E2E_KNOWLEDGE_TOKEN = (Get-Content "$env:USERPROFILE\minerva-e2e\secrets\minerva-knowledge-token.txt" -Raw).Trim()
```

Ключ приватного RAG у Minerva — лише з файлу (значення не показується):

```powershell
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\e2e.ps1 rag --url http://127.0.0.1:8788 --key-file <шлях>\private-api-key.txt
```

## Сценарій

### Автоматичний прогін (для регресій)

```powershell
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\e2e.ps1 scenario            # кроки 1–14
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\e2e.ps1 scenario --outage   # + крок 15 (зупиняє і запускає 8010)
```

Сценарій (`rag_assistant/e2e_scenario.py`) повторюваний:
- на початку відновлює вихідний стан (`baseline`);
- для кроків 6 і 13 створює власний лист прогону з TXT-вкладенням, а наприкінці повертає дані як були;
- після кожної зміни чекає, доки `applied_seq` RAG дорівняється `last_seq` і статуси відповідатимуть чинним ревізіям.

JSON-звіт пишеться в `%USERPROFILE%\minerva-e2e\reports\e2e-<run_id>.json`, без ключів і текстів листів. У ньому:
- кожна перевірка;
- кожен пошук: час, `document_revision`, score, вкладення, `dropped`;
- синхронізації;
- підсумок: p50 і max часу пошуку.

Якщо є збої, команда завершується з кодом 1. Логіку без RAG покривають тести `rag_assistant/tests_e2e_scenario.py`.

### Вручну

`e2e.ps1 status` показує статус індексації з RAG по кожному документу. `e2e.ps1 baseline` повертає вихідний стан. Після кожного кроку зачекайте, поки RAG забере зміни.

| # | Команда | Очікування |
|---|---|---|
| 1 | `status` | 6 документів `indexed`, у m3 — фрагменти вкладення |
| 2 | `search "Liefertermin KW 46 zugesagt"` | anna: m2 (і m1) |
| 3 | `search "Kalibrierprotokoll SN-77821"` / `--user e2e_boris` | anna: без m7 (лише власні листи); boris: лише m7 |
| 4 | `search "Steckverbinder SMA IP67"` | m3 (вкладення) |
| 5 | `search "Angebot ANT-2400" --project` | лише листи проєкту |
| 6 | `step new-mail`, потім `search "Expresszuschlag"` / `"Prüfbericht Vibrationstest"` | новий лист m8 і його вкладення |
| 7 | `step edit` (дата CAB-7 перемикається 14.11 ↔ 21.11), потім `search "CAB-7 Lieferung"` | лише чинна ревізія з новою датою |
| 8 | `step reassign`, потім `search "KW 46" --user e2e_boris` | boris бачить листи vertrieb; RAG не віддає їх для user_id anna; anna — «скриньки не підключені» |
| 9 | `step restore-owner` | права повертаються |
| 10 | `step deactivate` / `step activate` | tombstone `access_revoked` → пошук порожній → повернення |
| 11 | `step exclude` / `step include` | гілка CAB-7 зникає з пошуку і повертається |
| 12 | `step delete` / `step undelete`, `search "Rabatt 8 %"` | m5 зникає (`deleted`) і повертається |
| 13 | `step purge` | лист m8 і його вкладення зникли |
| 14 | `fallback --simulate` або зупинити RAG → `fallback` | усі OK: «недоступний» ≤ 3 с, пауза після 3 збоїв, «Історія» і стрічка працюють |
| 15 | крок під час зупинки RAG (або Minerva: `stop` 8010) → запустити знову → `rag` (скинути паузу) → `search` | RAG догнав журнал з курсора; поки Minerva недоступна — `last_error: unavailable`, курсор не рухається |

## Прибирання

```powershell
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\stop.ps1            # зупинити сервер і PostgreSQL
powershell -ExecutionPolicy Bypass -File rag_assistant\e2e\stop.ps1 -Remove    # видалити контейнер і тестові дані
```

Після видалення можна прибрати `%USERPROFILE%\minerva-e2e\`. З боку RAG окремо прибрати його тестову теку даних і колекцію `crm_correspondence_test`.
