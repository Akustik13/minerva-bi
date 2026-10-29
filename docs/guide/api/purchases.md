---
title: Закупівлі з CRM
order: 60
summary: Зовнішня CRM передає в Minerva замовлення постачальникам, трек-номери й «доставлено» — склад бачить «Очікується» і оприбутковує товар рівно один раз.
where: 🤖 API та скрипти → 📖 Документація API (розділ «Закупівлі з зовнішньої CRM»); у Minerva — 📦 Склад → 🗒️ Закупівлі (PO)
urls: /admin/inventory/purchaseorder/, /admin/inventory/incomingshipment/, /admin/inventory/inventorysettings/, /dashboard/api/docs/
roles: адміністратор, інтегратор CRM
keywords: закупівлі, purchase order, PO, CRM, постачальник, замовлення постачальнику, трек-номер, доставлено, оприбуткувати, очікується, incoming, external_id, delivery_id, дублі, Einkauf, Bestellung beim Lieferanten
related: inventory/purchases, api/webhooks, api/keys, api/console, inventory/settings
---

## Що це

Сценарій: товар у постачальників замовляє **ваша CRM**, а склад веде Minerva. CRM передає в Minerva, у кого, що,
скільки й коли замовили; потім трек-номер і орієнтовну дату; потім «доставлено». Склад у Minerva:

- одразу бачить товар у колонці **«Очікується»** (`incoming` у `/stock/`);
- після «доставлено» оприбутковує товар **один раз**, навіть якщо CRM повідомить двічі;
- закупівлю видно в [🗒️ Закупівлі (PO)](/admin/inventory/purchaseorder/) з позначкою, звідки вона.

Закупівлі, які створили в самій Minerva, через API лише читаються ([Закупівлі й постачальники](guide:inventory/purchases)).

## Підготовка

1. **Ключ** ([Ключі API](guide:api/keys)): **«Назва»** `crm`, права **«Закупівлі — … (читання)»** і
   **«Закупівлі — передати замовлення зовнішньої CRM…»** (`purchases:read`, `purchases:write`); для вебхука через
   API — ще `webhooks:read`, `webhooks:write`; щоб бачити залишки — `stock:read`. **«Джерело замовлень»** —
   назва системи, напр. `crm` (порожньо — теж `crm`).
2. **Артикули** в CRM мають збігатися з Minerva або бути аліасами ([аліаси товарів](/admin/inventory/productalias/)).
   На час запуску передавайте `"strict": true` — невідомий артикул тоді повертає `404 sku_not_found` і нічого
   не зберігає.
3. **Що робити зі складом** — [⚙️ Налаштування складу](/admin/inventory/inventorysettings/) →
   **«Зовнішня система повідомила «доставлено»»**:
   - **«Одразу оприбуткувати на склад (без дублікатів)»** — за замовчуванням;
   - **«Лише позначити «прибуло» — оприбуткує склад у Minerva»** — товар приймає склад у Minerva вручну.

## Як Minerva не допускає дублів

| Що | За чим впізнає | Повтор |
|---|---|---|
| Замовлення | джерело + `external_id` (номер у CRM) | оновлює те саме замовлення (`200`, `"created": false`) |
| Позиція | `line_id` (номер рядка в CRM), інакше артикул | оновлює кількість / ціну рядка |
| Відправлення | трек-номер | одне вхідне відправлення на номер |
| Доставка | `delivery_id` (номер накладної), інакше трек-номер, інакше «уся решта» | `200`, `"duplicate": true`, на склад нічого |
| Забагато | отримано більше, ніж замовлено | `409 over_receipt` — найчастіше та сама доставка під іншим номером |

Тож CRM при збої мережі, 429 чи 5xx просто повторює **той самий** запит.

## Повний цикл — приклад

Замовили 10 шт FT220407 і 25 шт CA-SMA-1M у постачальника; приїхало двома доставками.

**1. Замовлення підтверджено** — передати (той самий запит пізніше = оновити):

```bash
curl -X POST "{{base_url}}/api/v1/purchase-orders/" \
  -H "Authorization: Token $MINERVA_KEY" -H "Content-Type: application/json" \
  -d '{"external_id": "B-2026-0042",
       "supplier": {"name": "Acme Components GmbH", "email": "sales@acme.example"},
       "supplier_order_no": "AB-7781", "order_date": "2026-10-20", "expected_date": "2026-11-05",
       "currency": "EUR",
       "lines": [{"line_id": "1", "sku": "FT220407", "qty": 10, "unit_price": "12.50"},
                 {"line_id": "2", "sku": "CA-SMA-1M", "qty": 25}]}'
```

Відповідь `201`: закупівля `PO-57`, `status: ordered`; на складі «очікується» FT220407 +10, CA-SMA-1M +25.
Постачальник шукається за назвою, потім email, інакше створюється. Невідомий артикул без `strict` — позиція без
товару й попередження `sku_not_found` у `warnings`.

**2. Трек-номер** (номер у CRM у адресі — `ext:<номер>`):

```bash
curl -X POST "{{base_url}}/api/v1/purchase-orders/ext:B-2026-0042/shipments/" \
  -H "Authorization: Token $MINERVA_KEY" -H "Content-Type: application/json" \
  -d '{"tracking_number": "1Z999AA10123456784", "carrier": "UPS", "expected_date": "2026-11-03"}'
```

`status` відправлення: `pending`, `in_transit`, `customs`, `arrived`, `exception`, `delivered`. Відправлення видно
в [🚢 Трекінг приходу](/admin/inventory/incomingshipment/), а його дата стає орієнтовною датою закупівлі.

**3. Приїхала частина** (накладна LS-118):

```bash
curl -X POST "{{base_url}}/api/v1/purchase-orders/ext:B-2026-0042/deliveries/" \
  -H "Authorization: Token $MINERVA_KEY" -H "Content-Type: application/json" \
  -d '{"delivery_id": "LS-118", "delivered_at": "2026-11-04", "lines": [{"line_id": "1", "qty": 4}]}'
```

`201`, `delivery.booked: true`, `status: partial`; на складі FT220407 +4, очікується ще 6.

**4. Те саме «доставлено» прийшло вдруге** — `200`, `delivery.duplicate: true`, склад без змін.

**5. Та сама доставка під іншим номером** з кількістю 9 — `409 over_receipt` (лишилось лише 6). Якщо справді
привезли більше — передайте `"allow_over": true`.

**6. Решта** — `{"delivery_id": "LS-131"}` без позицій = уся решта: `status: received`, FT220407 +6, CA-SMA-1M +25.

> 💡 Частинами — завжди з `delivery_id` (або трек-номером), інакше `400 delivery_id_required`: так Minerva
> відрізняє повтор від нової доставки.

## Інші дії

- **Змінити** — `PATCH /purchase-orders/{ref}/` (нова дата, номер підтвердження постачальника, позиції);
  `qty: 0` — позицію скасовано; кількість не може стати меншою за вже отриману (`409 qty_below_received`).
- **Скасувати** — `POST /purchase-orders/{ref}/cancel/` з `{"reason": "…"}`: решта більше не очікується,
  отримане лишається.
- **Знайти** — `GET /purchase-orders/?open=1` (ще очікуються), фільтри `status`, `supplier`, `sku`,
  `tracking_number`, `changed_since` (для синхронізації).
- `{ref}` — код Minerva (`PO-57`) або `ext:<номер у CRM>`. Номер зі «/» — `POST /purchase-orders/shipments/` чи
  `…/deliveries/` з `external_id` у тілі.

## Зворотний зв'язок: вебхук для CRM

Щоб CRM дізнавалась, коли склад сам прийняв товар у Minerva, змінився стан чи трекінг, — вебхук з подією
`purchase_order.updated` (вибрати явно):

```json
POST /api/v1/webhooks/
{"name": "crm", "url": "https://crm.example/minerva-hook", "events": ["purchase_order.updated"]}
```

У події є `changed_by`: джерело ключа (`crm`) — це зміна самої CRM, її обробник пропускає; `minerva` — зміну
зробили в Minerva. Страховка — раз на годину чи добу `GET /purchase-orders/?changed_since=<час минулого запуску>`.
Детально — [Вебхуки](guide:api/webhooks).

## Що видно в Minerva

- [🗒️ Закупівлі (PO)](/admin/inventory/purchaseorder/) — колонка й фільтр **«Звідки»** (джерело й номер у CRM);
  у картці — **«📦 Доставки, про які повідомила зовнішня система (API)»**.
- Огляд складу — колонка «Очікується».
- [📜 Журнал API](/admin/api/apirequestlog/) — кожен запит CRM з номером закупівлі.

## Перевірка без CRM

- Скрипт **crm_purchases.py** ([📖 Документація API](/dashboard/api/docs/) → **«⬇ crm_purchases.py (закупівлі CRM)»**):
  `python crm_purchases.py demo FT220407` проходить увесь цикл на тестовому товарі й показує склад на кожному
  кроці; окремі команди — `order`, `get`, `list`, `track`, `delivered`, `cancel`, `sync`.
- Консоль Minerva — пресети **«📦 Закупівля CRM»**, **«🚛 Трек закупівлі»**, **«✅ Доставлено»**, **«📋 Очікуються»**
  ([Консоль API](guide:api/console)).

## Адреси

{{api_endpoints:purchase-orders}}

## Часті питання та проблеми

**`403 not_external`** — закупівлю створили в Minerva; через API її можна лише читати.

**`404 sku_not_found`** при `strict` — артикул CRM не знайдено: виправте або додайте аліас.

**`409 over_receipt`** — доставка більша за замовлене: перевірте, чи це не повтор під іншим номером.

**«Доставлено» прийшло, а на складі нічого** — у налаштуваннях складу вибрано **«Лише позначити «прибуло»»**
(відповідь `booked: false`), або в закупівлі кілька відправлень, а позиції не передано (`warnings: lines_needed`).
