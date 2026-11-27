---
title: 🏭 Постачальники, 🪪 візитки й 📨 запити через API та MCP
order: 77
summary: Зовнішній агент (Claude Code, Claude.ai, ваша програма) працює з постачальниками, візитками й запитами постачальникам так само, як ви на сторінках «Усі постачальники», «🪪 Усі візитки», «📨 Новий запит» і «📨 Запити (RFQ)» — фільтри, картки, нові постачальники й контактні особи, візитки з фото, копія запиту з файлами; усе, що змінює дані, — лише після вашого ✓.
where: 🤖 API та скрипти → REST API → 🔑 API ключі → ключ з правами «Агент (MCP)»; 📦 Склад → 🤖 Завдання агенту
urls: /admin/inventory/supplier/, /admin/inventory/supplier/cards/, /admin/suppliers/supplierrequest/, /admin/suppliers/supplierrequest/new/, /admin/agent/agenttask/
roles: адміністратор (ключ), складник і менеджер (перевірка пропозицій)
keywords: постачальники через API, постачальники через MCP, find_suppliers, supplier_filters, propose_supplier, propose_supplier_contact, propose_supplier_check, find_suppliers_in_mail, supplier_from_mail, find_cards, get_card, card_stats, propose_card, propose_new_card, supplier_request_form, preview_rfq_letter, копія запиту через агента, copy_from, файли до запиту, візитка через агента, агент прочитав візитку, фото візитки, vCard, /api/v1/suppliers/, /api/v1/supplier-requests/, /api/v1/supplier-requests/<RFQ>/table/, propose_rfq_columns, propose_rfq_column_values, propose_rfq_marks, /api/v1/agent/tools/, REST для інструментів агента, Lieferant API, Visitenkarte, RFQ API
related: api/mcp, api/files, inventory/suppliers, inventory/supplier-requests, inventory/cards-app, inventory/agent-tasks, api/keys
---

## Що це

Через [MCP](guide:api/mcp) і REST API агент бачить і пропонує те саме, що ви робите на чотирьох сторінках:

| Сторінка | Що може агент |
|---|---|
| [Усі постачальники](/admin/inventory/supplier/) | шукати з тими самими фільтрами (категорія з підкатегоріями або «без категорії», стан, звідки — 🎪 з виставки, де взяли, країна, є email, оцінка), читати картку; запропонувати **нового постачальника** чи **зміну картки** (стан, оцінка, категорії, адреса, умови, що роблять, нотатки, запис в «🕘 Історію»), **контактну особу**, дії для вибраних — 🧵 історія з пошти, 🧠 AI-оцінка, 🌐 пошук в інтернеті |
| [🪪 Усі візитки](/admin/inventory/supplier/cards/) | шукати з фільтрами сторінки, читати візитку, **дивитися фото** (і самому прочитати з них дані — без платного AI Minerva), статистика по подіях; запропонувати змінити візитку, внести її в постачальники чи CRM, видалити, прочитати платним AI; **нову візитку з фото** |
| [📨 Новий запит](/admin/suppliers/supplierrequest/new/) | бачити форми й поля, мови, змінні листа, категорії, групи, проєкти; **👁 переглянути лист** одному постачальнику; запропонувати новий запит, **📋 копію** іншого (поля, кому, файли — що зазвичай треба змінити, вам підсвітиться) і **файли** (креслення, BOM, Gerber — з Minerva або свої) |
| [📨 Запити (RFQ)](/admin/suppliers/supplierrequest/) | шукати за станом, назвою, проєктом, постачальником, категорією, групою, датою; бачити, з якого скопійовано й що ще перевірити після копіювання; далі — усе, що описано в [Запитах постачальникам](guide:inventory/supplier-requests) |

Агент працює **від імені людини ключа** і з її правами: без права змінювати постачальників не побачить
пропозицій змін; 🔒 приватних візиток інших людей не бачить ніхто.

> ⚠️ Нічого не змінюється, доки ви не перевірите й не застосуєте ✓ у [🧩 Завданнях агенту](/admin/agent/agenttask/).
> Після ✓ Minerva робить те саме, що кнопки сторінок; якщо ви змінили картку раніше за ✓ — пропозиція не
> спрацює, агент запропонує заново.

## Що спитати агента

- «Знайди виробників плат з Китаю, з якими ми ще не працювали» → `find_suppliers` (category, country, status).
- «Скільки візиток з Electronica 2026 ще не внесено? Внеси ті, що вже є в базі» → `card_stats`, `find_cards`
  (show inbase), `propose_card` to_supplier.
- «Подивись фото візитки #42 і заповни, що на ній» → `get_card`, `get_file` (фото), `propose_card` edit з `data`.
- «Сфотографував візитку — ось файл» (Claude Code) → `upload_link`, `propose_new_card` з полями з фото.
- «Створи постачальника з цього листа» (🔒 особистий ключ) → `supplier_from_mail`, `propose_supplier` з `message`.
- «Зроби копію RFQ-2026-003 на 1000 шт і додай новий Gerber» → `propose_supplier_request` з `copy_from`,
  `quantities`, `files`.
- «Покажи, як піде лист PCBWay» → `preview_rfq_letter`.

## Інструменти MCP

| Інструмент | Що робить | Право ключа |
|---|---|---|
| `find_suppliers`, `supplier_filters`, `get_supplier` | пошук з фільтрами списку; значення фільтрів з кількостями; картка | `agent:read` |
| `propose_supplier` | новий постачальник (з контактами, категоріями; з листа — `message`) або зміна картки, запис в історію | `agent:write` |
| `propose_supplier_contact` | нова контактна особа, зміна, ⭐ основний, ⛔ уже не працює там | `agent:write` |
| `propose_supplier_check` | 🧵 історія з пошти / 🧠 AI-оцінка (🔒 особистий ключ) / 🌐 пошук в інтернеті для вибраних — після ✓ у фоні | `agent:write` |
| `find_suppliers_in_mail`, `supplier_from_mail` | 🔒 хто з вашого листування схожий на постачальника; чернетка постачальника з листа | `agent:read` |
| `find_cards`, `get_card`, `card_stats` | візитки з фільтрами; візитка з фото (ref `card:N`), vCard (`vcard:N`); статистика | `agent:read` |
| `propose_card`, `propose_new_card` | змінити / у постачальники / у CRM / видалити / прочитати AI; нова візитка з фото | `agent:write` |
| `supplier_request_form`, `preview_rfq_letter` | що є у формі нового запиту; 👁 лист одному постачальнику | `agent:read` |
| `propose_supplier_request` | новий запит, 📋 `copy_from`, `files` / `remove_files`, `contacts` | `agent:write` |

Фото візиток і файли запитів агент завантажує через [📎 файли](guide:api/files): `list_files` з `card` чи
`request`, далі `get_file`.

> 💡 Платний AI Minerva (🧠 прочитати візитку, 🧠 оцінка, 🌐 інтернет) запускається лише після вашого ✓ — у
> пропозиції написано, скільки це приблизно коштує. Агент може й сам прочитати фото візитки й запропонувати поля —
> тоді AI Minerva не потрібен.

## REST API

Ключ — той самий, що для MCP (`Authorization: Token <ключ>`, право `agent:read`):

| Адреса | Що повертає |
|---|---|
| `GET /api/v1/suppliers/?category=&status=&source=&where=&country=&q=` | постачальники (JSON, `limit`, `offset`) |
| `GET /api/v1/suppliers/filters/` | категорії, стани, звідки, де взяли, країни |
| `GET /api/v1/suppliers/<id>/` | картка: контакти, закупівлі, запити, історія, аналізи |
| `GET /api/v1/suppliers/cards/?show=new&event=` | візитки |
| `GET /api/v1/suppliers/cards/stats/` | статистика візиток по подіях |
| `GET /api/v1/suppliers/cards/<id>/` | візитка: прочитане, люди, фото (ref) |
| `GET /api/v1/supplier-requests/?status=all&group=` | запити постачальникам |
| `GET /api/v1/supplier-requests/form/` | форми й поля нового запиту |
| `GET /api/v1/supplier-requests/RFQ-2026-003/` | запит: поля, лист, файли, отримувачі й пропозиції, свої колонки таблиці (`columns`) і значення кожного рядка, 🏷 позначка рядка (`label`) |
| `GET /api/v1/supplier-requests/RFQ-2026-003/table/` | таблиця пропозицій, як на сторінці: кількості, колонки (стандартні й свої), рядки (📌 закріплені першими) — числами: ціна за шт. і сума з разовими у валюті постачальника й Minerva, 🥇 / ⚡, що враховано в сумі (`in_sum`: y / n / u), свої колонки (`source`: ai / user / agent), довідник кольорів і наліпок |

Змінювати через REST — **тими самими інструментами**: `GET /api/v1/agent/tools/` — що доступно ключу,
`POST /api/v1/agent/tools/<назва>/` з аргументами інструмента (JSON) → `{text, is_error, review_url}`. Читання —
`agent:read`, `propose_*` — `agent:write`; пропозиції лягають у завдання ключа на сьогодні, `review_url` — де їх
перевірити.

```
curl -H "Authorization: Token <ключ>" -H "Content-Type: application/json" \
     -d '{"name": "Rosenberger", "website": "rosenberger.com", "status": "potential", "evidence": "сайт"}' \
     {{base_url}}/api/v1/agent/tools/propose_supplier/
```

## Часті питання та проблеми

**«схоже, він уже є: #N …».** Такий постачальник уже в Minerva (той самий email, домен чи сайт) — попросіть
агента змінити його картку. Якщо це справді інша фірма — `allow_duplicate: true`.

**`find_suppliers_in_mail` немає в списку.** Це ваша пошта — лише 🔒 [особистий ключ](guide:api/keys) і власна
скринька в Minerva.

**«це фото іншої людини…».** Змінювати чужі візитки може лише той, хто має право змінювати постачальників; з правом
лише додавати — свої фото.

**Агент не бачить візитку.** Вона 🔒 «лише я» в іншої людини — її не бачить ніхто, крім того, хто зняв.

**Пропозиція «не вдалося: …змінили після пропозиції».** Картку змінили раніше за ваш ✓ — попросіть агента
запропонувати заново.
