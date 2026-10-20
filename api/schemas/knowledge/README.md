# Minerva knowledge API v1 — JSON-схеми (proposal MIN-002 / M8)

**Статус:** proposal Minerva для `integration/API_CONTRACT.md` draft 0.3. Це не accepted.
- Wire-контракт буде погоджено, коли зійдуться обидва набори схем (ці — Minerva, `/v2/*` — RAG) і їх перевірять на синтетичних прикладах.
- `contract_version`: `minerva-knowledge/1.0-draft`.
- Схеми: JSON Schema draft 2020-12, файли `*.schema.json` поруч.

**Перевірка:** `python api/schemas/knowledge/validate.py`. Потрібен `jsonschema>=4.18` (див. `requirements-dev.txt`).
- Правильні приклади (`examples/valid/`) мають пройти і схему, і семантичні правила. Неправильні (`examples/invalid/`) мають упасти.
- Усі приклади синтетичні.

## Доступ
- `Authorization: Token <ключ>` — окремий `APIKey` Minerva.
- Права ключа:
  - `knowledge:read` — `changes`, `documents`, вкладення, `snapshots`, `summary`;
  - `knowledge:write` — `status`.
- Ключ, прив'язаний до магазину, → `403 shop_key_forbidden`. Сесія браузера не дає доступу до цих маршрутів.
- `MINERVA_INSTANCE_ID` не задано → `503 instance_not_configured` на всіх маршрутах.

## Маршрути

| Метод | Шлях | Відповідь | Права |
|---|---|---|---|
| GET | `/api/v1/knowledge/changes/?after=<seq>&limit=<1..200>[&generation=<uuid>]` | `changes.response` | read |
| GET | `/api/v1/knowledge/documents/<document_id>/` | `document` | read |
| GET | `/api/v1/knowledge/documents/<document_id>/attachments/<index>/?revision=<n>` | байти файлу | read |
| POST | `/api/v1/knowledge/manifest/snapshots/` | `snapshot.create.response` (201) | read |
| GET | `/api/v1/knowledge/manifest/snapshots/<snapshot_id>/?cursor=<opaque>&limit=<1..5000>` | `snapshot.page.response` | read |
| POST | `/api/v1/knowledge/status/` | тіло `status.request`, відповідь `status.response` | write |
| GET | `/api/v1/knowledge/summary/` | `summary.response` | read |

**Помилки:** будь-яка не-2xx відповідь — це JSON `error.schema.json`. Відповідь іншого формату (наприклад, HTML 404 проксі) не є підставою видаляти щось з індексу.

| HTTP | `code` | Дія RAG |
|---|---|---|
| 400 | `invalid_request` | виправити запит, не повторювати |
| 401 | `unauthorized` | зупинити синхронізацію |
| 403 | `forbidden_scope`, `shop_key_forbidden` | зупинити синхронізацію |
| 404 | `document_not_found` | tombstone документа |
| 404 | `attachment_not_found` | позначити лише вкладення; лист не видаляти, перечитати документ |
| 404 | `snapshot_not_found` | створити новий знімок, нічого не видаляти |
| 409 | `cursor_ahead` (`details.last_seq`) | курсор новіший за журнал (відновлення БД) → повна звірка через snapshot |
| 409 | `generation_mismatch` (`details.generation`) | змінилося покоління → повна звірка через snapshot |
| 410 | `cursor_expired` (`details.resume_after`) | повна звірка через snapshot, потім `after = snapshot_seq` |
| 410 | `snapshot_expired` | нічого не видаляти, створити новий знімок |
| 412 | `revision_mismatch` (`details.current_revision`) | перечитати документ |
| 429 | `rate_limited` + `Retry-After` | пауза |
| 500 | `internal_error` | повтор із паузою, нічого не видаляти |
| 503 | `instance_not_configured` | повтор із паузою, нічого не видаляти |

## Вкладення
- Відповідь: `200` з байтами, `Content-Type` вкладення, `X-Content-SHA256: <hex>`, `ETag: "sha256:<hex>"`, `Content-Disposition: attachment`.
- Без редиректів; файли віддаються лише з цього маршруту Minerva.
- `?revision` обов'язковий. Якщо він не дорівнює чинній ревізії документа → `412 revision_mismatch`.
- `content_hash` у документі завжди збігається з `X-Content-SHA256` для цієї ревізії. `indexable: true` можливе лише з `content_hash` і `url`.
- Ще не завантажене з IMAP вкладення → `indexable: false, reason: not_downloaded`. Після фонового завантаження ревізія документа зростає.

## Гарантії Minerva

1. **Порядок commit.**
   - Кожна транзакція, що пише журнал, першою дією бере `pg_advisory_xact_lock(knowledge)`; на SQLite запис серіалізує сама БД.
   - У тій самій транзакції беруться `seq` і пишуться події журналу разом зі станом документів (ревізія, `content_version`, tombstone).
   - Блокування звільняється на commit, отже порядок `seq` = порядок commit. Після того як видно подію N, подія ≤ N вже не з'явиться.
   - Тест зворотного порядку commit двох транзакцій — лише на PostgreSQL.
2. **Snapshot.** `snapshot_seq` і склад читаються в одній транзакції `REPEATABLE READ`. Склад = стан після подій ≤ `snapshot_seq`. Знімок незмінний, живе 24 год.
3. **`document_revision`** — монотонна для кожного документа:
   - +1 за кожну зміну `content_version` (зокрема ACL), tombstone чи повернення після tombstone;
   - не зменшується ніколи: стан документа не видаляється навіть після жорсткого видалення листа.
4. **Стрічка змін.**
   - У межах сторінки кожен `document_id` — лише раз (остання подія).
   - `document_revision` у події — чинна ревізія на момент читання. Якщо стан змінився без сигналу, Minerva спершу підвищує ревізію, потім віддає документ.
   - Документ, що вже не допущений до індексації, віддається як `delete`.
5. **Пауза** (`enabled: false`):
   - нові документи не додаються;
   - tombstone, відкликання доступу й зміни вмісту вже переданих документів надходять далі;
   - знімок у паузі містить лише досі дозволені передані документи.
   - «Прибрати все з індексу» — окрема дія: tombstone для всіх документів.
6. **`text_new` у v1:** `text_new_mode = "full"`, `text_new = text[:20000]` (цитати лишаються). Обрізання позначається прапорцями `*_truncated`. `reply_trimmed` зарезервовано.
7. **Ідентичність:** `source_instance_id` (env) + `generation` (UUID у БД) — у кожній відповіді й документі. RAG не змішує документи різних джерел і поколінь.
8. **Журнал** зберігається 30 днів. Старший курсор → `410 cursor_expired`.

## Статуси (`POST /status/`)
- Прийом за ключем (`document_id`, `document_revision`, `attempt_id`):

| Результат | Коли |
|---|---|
| `accepted` | статус прийнято |
| `duplicate` | `attempt_id` дорівнює вже прийнятому — ідемпотентно |
| `stale_attempt` | `attempt_id` менший за прийнятий |
| `stale` | ревізія менша за чинну |
| `revision_ahead` | ревізія більша за чинну — помилка узгодженості, Minerva логує її |
| `instance_mismatch` | інше джерело або покоління |
| `unknown_document` | документа не було в стрічці |

- Для однієї ревізії перехід визначає лише `attempt_id`: новіша спроба перекриває старішу будь-яким станом, тож запізнілий `failed` не перекриває новіший `indexed`.
- `deleted` — лише для ревізії tombstone, без `content_version`.
- Код відповіді — `200` з результатом для кожного запису. `400` — лише якщо зіпсовано весь запит.
