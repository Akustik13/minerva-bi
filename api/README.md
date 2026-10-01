# Minerva REST API v1

Базовий URL: `https://<ваш-домен>/api/v1/` (локально `http://localhost:8000/api/v1/`)

## 1. Підключення

1. **Адмін → REST API → API Ключі → Додати.**
   - Назва: напр. `webshop`
   - Права: оберіть потрібні scopes (див. таблицю)
   - Джерело замовлень: `webshop` — цей `source` отримають замовлення, якщо в запиті його не вказано
2. Скопіюйте ключ (розділ «Технічна інформація»).
3. Кожен запит містить заголовок:
   ```
   Authorization: Token <ключ>
   ```
4. Перевірка: `GET /api/v1/ping/` → назва ключа і його права.

> ⚠️ Ключ використовується **тільки на сервері магазину** (PHP/Node/Python). Не вставляйте його в JavaScript сторінки — його побачить кожен відвідувач.

| Scope | Що дозволяє |
|---|---|
| `stock:read` | залишки, журнал рухів, локації, перевірка кошика |
| `stock:write` | прихід, списання, коригування, інвентаризація |
| `products:read` / `products:write` | каталог товарів, категорії |
| `orders:read` / `orders:write` | замовлення: читання / створення, зміна статусу, скасування |
| `shipments:read` | відправлення, трекінг-номери, статус доставки |
| `customers:read` / `customers:write` | клієнти CRM |
| `webhooks:read` / `webhooks:write` | вебхуки, зареєстровані цим ключем |

**Ліміт:** 120 запитів/хв на ключ (змінна оточення `API_RATE_LIMIT`, напр. `300/min`). При перевищенні — `429` + заголовок `Retry-After`.
**Пагінація:** `?page=2&page_size=200` (макс. 500). Відповідь: `{count, next, previous, results}`.
**Сортування:** `?ordering=-available`, `?ordering=sku`.
**DELETE** не підтримується ніде (405).

---

## 2. Склад

### Що означають цифри

| Поле | Значення |
|---|---|
| `on_hand` | фізично на складі (всі рухи, крім броні) |
| `reserved` | заброньовано під замовлення (режим «Бронювати замовлення» в налаштуваннях складу) |
| `available` | **можна продати** = `on_hand − reserved` ← це показуйте в магазині |
| `incoming` | очікується від постачальників (відкриті закупівлі) |
| `in_stock` | `available > 0` |
| `low_stock` | `available ≤ reorder_point` (якщо точку дозамовлення задано) |
| `last_movement_at` | час останнього руху по товару |

### `GET /stock/` — залишки всіх товарів
Фільтри:
- `sku=A,B,C` — конкретні SKU
- `category=<slug>`, `kind=finished|component`, `is_active=true`
- `search=amp` — пошук по SKU / назві / виробнику
- `in_stock=true|false`, `low_stock=true`
- `changed_since=2026-10-01T12:00:00Z` — **тільки товари з рухами після дати** (інкрементальна синхронізація)
- `location=MAIN` — рахувати залишки лише на одній локації

```json
{
  "count": 1, "next": null, "previous": null,
  "results": [{
    "sku": "AMP-100", "name": "Amplifier", "category": "audio", "unit_type": "piece",
    "is_active": true, "on_hand": 10, "reserved": 2, "available": 8, "incoming": 50,
    "in_stock": true, "low_stock": false, "reorder_point": 5, "lead_time_days": 14,
    "sale_price": 99.0, "last_movement_at": "2026-10-01T09:15:02Z"
  }]
}
```

### `GET /stock/{sku}/` — один товар
Те саме + `locations` (розбивка по складах) і `buildable` (скільки можна зібрати з компонентів для товарів з BOM). Працюють і аліаси SKU.
Для SKU зі `/` використовуйте `GET /stock/?sku=...`.

### `POST /stock/check/` — перевірка кошика (scope `stock:read`)
```json
{"items": [{"sku": "AMP-100", "qty": 2}, {"sku": "CABLE-M", "qty": 5.5}]}
```
→
```json
{"ok": false, "items": [
  {"sku": "AMP-100", "product_sku": "AMP-100", "found": true, "requested": 2, "available": 8, "ok": true},
  {"sku": "CABLE-M", "product_sku": "CABLE-M", "found": true, "requested": 5.5, "available": 3, "ok": false}
]}
```

### `POST /stock-movements/` — прихід / списання / коригування
```json
{
  "sku": "AMP-100",
  "tx_type": "Incoming",          // Incoming | Outgoing | Adjustment
  "qty": 10,                      // Incoming/Outgoing — додатне; Adjustment — зі знаком (-2 = списати 2)
  "location": "MAIN",             // необов'язково — локація за замовчуванням з налаштувань складу
  "ref_doc": "Lieferschein 4711", // необов'язково
  "external_key": "grn-4711-1",   // необов'язково, але РЕКОМЕНДОВАНО: повтор не створить дубль
  "tx_date": "2026-10-01T10:00:00Z"
}
```
- `201` — створено, `200` — рух з таким `external_key` вже існує (повернуто існуючий)
- Відповідь містить `available_after` — залишок після руху
- `400` — дробова кількість для штучного товару, невірна локація (компонент на склад готової продукції тощо)
- `409 insufficient_stock` — якщо в налаштуваннях складу заборонено від'ємний залишок

### `GET /stock-movements/` — журнал рухів
Фільтри: `sku=A,B`, `tx_type=Incoming|Outgoing|Adjustment|Reserved`, `location=MAIN`, `date_from`, `date_to`, `created_after` (для синхронізації), `ref_doc` (містить текст).

### `POST /stock/count/` — інвентаризація
Встановлює **фактичний** залишок; Minerva створює коригування на різницю. Один об'єкт або список (атомарно — помилка в одному рядку скасовує всі):
```json
[{"sku": "AMP-100", "qty": 7, "location": "MAIN"}, {"sku": "CABLE-M", "qty": 120.5}]
```
→ `{"results": [{"sku": "AMP-100", "location": "MAIN", "previous": 10, "counted": 7, "delta": -3, "transaction_id": 812}, ...]}`

### `GET /locations/`, `GET /categories/`
Довідники складів та категорій товарів.

---

## 3. Товари

`GET /products/` — каталог (ціни, вага, HS-код, країна походження, `image_url`, `datasheet_url`).
- `?with_stock=1` — додає поля залишків (як у `/stock/`)
- Ті ж фільтри, що й у `/stock/`
- `POST /products/`, `PATCH /products/{id}/` — scope `products:write`

---

## 4. Замовлення з магазину

### `POST /orders/` — створити замовлення
```json
{
  "order_number": "WS-1001",        // номер у магазині — обов'язково
  "source": "webshop",              // необов'язково: інакше «Джерело замовлень» ключа, інакше "api"
  "order_date": "2026-10-01",       // необов'язково: сьогодні
  "currency": "EUR",
  "client": "Max Mustermann",       // покупець (ім'я або компанія)
  "contact_name": "Max Mustermann",
  "email": "max@example.com",
  "phone": "+49 30 123456",
  "buyer_vat_id": "",
  "addr_street": "Hauptstr. 1", "addr_city": "Berlin", "addr_zip": "10115",
  "addr_state": "", "addr_country": "DE",
  "ship_name": "", "ship_company": "", "ship_phone": "", "ship_email": "",
  "shipping_cost": 4.90,
  "shipping_deadline": "2026-10-03",
  "note": "Подарункова упаковка",   // → внутрішня нотатка
  "check_stock": true,              // відхилити, якщо не вистачає товару
  "lines": [
    {"sku": "AMP-100", "qty": 2, "unit_price": 99.00},
    {"sku": "CABLE-M", "qty": 5.5}  // без ціни → береться sale_price товару
  ]
}
```
Що відбувається:
1. SKU розпізнаються (SKU або аліас). Невідомий SKU → `400 invalid_lines` зі списком рядків, замовлення **не** створюється.
2. `check_stock: true` (або заборона від'ємного залишку в налаштуваннях) → `409 insufficient_stock`, якщо не вистачає.
3. Замовлення створюється атомарно → склад списується/бронюється за правилами **Налаштувань складу** (при створенні / при відправці / бронювання), клієнт автоматично з'являється в CRM, приходить сповіщення (email/Telegram).
4. `total_price` рахується з рядків, якщо не передано.
5. **Ідемпотентність:** повторний POST з тим самим `source + order_number` не створює дубль — повертає існуюче замовлення з `"created": false` і кодом `200`. Тому магазин може безпечно повторювати запит при таймауті.

Відповідь `201`: повне замовлення з `lines`, `shipments` і `"created": true`.

### Інші операції
| Запит | Опис |
|---|---|
| `GET /orders/?order_number=WS-1001&source=webshop` | знайти замовлення за номером магазину |
| `GET /orders/?status=shipped&date_from=2026-10-01` | фільтри: `status`, `source`, `email`, `date_from`, `date_to` |
| `GET /orders/{id}/` | деталі + рядки + відправлення з трекінгом |
| `PATCH /orders/{id}/` | змінити поля, напр. `{"status": "processing"}` |
| `POST /orders/{id}/cancel/` | скасувати; товар повертається на склад (`{"restock": false}` — не повертати). Відправлені/доставлені — `409` |

Статуси: `received` → `processing` → `shipped` → `delivered`, або `cancelled`.

---

## 5. Доставка

`GET /shipments/` — відправлення (scope `shipments:read`).
Фільтри: `order_number`, `source`, `order` (id), `status`, `tracking_number`, `created_after`.

```json
{
  "id": 41, "order": 1203, "order_source": "webshop", "order_number": "WS-1001",
  "status": "in_transit", "carrier": "UPS Standard", "carrier_type": "ups",
  "carrier_service": "UPS Standard", "tracking_number": "1Z999AA10123456784",
  "tracking_url": "https://www.ups.com/track?tracknum=1Z999AA10123456784&requester=WT/trackdetails",
  "carrier_status_label": "Unterwegs", "carrier_delayed": false,
  "eta_from": "2026-10-03", "eta_to": "2026-10-04", "delivered_at": null, ...
}
```
Статуси: `draft`, `submitted`, `label_ready`, `in_transit`, `delivered`, `error`, `cancelled`.

---

## 6. Вебхуки (Minerva → магазин)

Замість опитування магазин отримує POST при кожній зміні.

| Подія | Коли | `data` |
|---|---|---|
| `stock.changed` | змінився залишок (прихід, продаж, коригування, скасування) | `{"items": [<як у /stock/>]}` — усі товари однієї транзакції БД в одній події |
| `order.created` | створено замовлення (з будь-якого джерела) | `id, source, order_number, status, order_date, total_price, currency, …` |
| `order.status_changed` | змінився статус замовлення | те саме + `previous_status` |
| `shipment.updated` | з'явився трекінг-номер або змінився статус відправлення | як у `/shipments/` + `previous_status` |
| `ping` | тест з адмінки або `POST /webhooks/{id}/test/` | `{"message": …}` |

**Реєстрація:** Адмін → REST API → Вебхуки, або через API:
```json
POST /api/v1/webhooks/
{"name": "shop", "url": "https://shop.example/minerva-hook",
 "events": ["stock.changed", "order.status_changed", "shipment.updated"],
 "order_sources": "webshop"}
```
У відповіді — `secret` (`whsec_…`) для перевірки підпису. `order_sources` обмежує `order.*`/`shipment.*` замовленнями лише цих джерел.
`GET /webhooks/` — лише вебхуки цього ключа; `PATCH`/`DELETE /webhooks/{id}/`; `POST /webhooks/{id}/test/`; `GET /webhooks/{id}/deliveries/` — останні 50 доставок.

**Запит до магазину:**
```
POST https://shop.example/minerva-hook
Content-Type: application/json
X-Minerva-Event: stock.changed
X-Minerva-Delivery: 0b6f…            ← однаковий при повторах — використовуйте для дедуплікації
X-Minerva-Signature: t=1759312345,v1=5d1c…

{"id": "0b6f…", "event": "stock.changed", "created_at": "2026-10-01T12:00:00+00:00",
 "data": {"items": [{"sku": "AMP-100", "available": 8, …}]}}
```

**Перевірка підпису:** `v1 = HMAC_SHA256(secret, "<t>.<сире тіло запиту>")` у hex; відхиляйте, якщо `t` старше 5 хв. Приклади: Python — [`examples/webhook_receiver.py`](examples/webhook_receiver.py); PHP:
```php
$body = file_get_contents('php://input');
parse_str(str_replace(',', '&', $_SERVER['HTTP_X_MINERVA_SIGNATURE'] ?? ''), $sig);
$expected = hash_hmac('sha256', $sig['t'] . '.' . $body, getenv('MINERVA_WEBHOOK_SECRET'));
if (!hash_equals($expected, $sig['v1'] ?? '') || abs(time() - (int)$sig['t']) > 300) {
    http_response_code(401); exit;
}
```

**Доставка і повтори:** відповідь `2xx` за ≤ 10 с = доставлено. Інакше повтори через 1 хв, 5 хв, 30 хв, 2 год, 6 год, 12 год, 24 год (команда `send_webhooks` у контейнері `cron`), далі — статус «Помилка». Журнал і ручний повтор: Адмін → REST API → Журнал вебхуків.

> Порядок подій не гарантований (повтори). У `stock.changed` завжди актуальний залишок на момент події — просто перезаписуйте його. Масові зміни через «оновлення списком» в адмінці (queryset.update) подій не генерують — для страховки раз на добу робіть повну синхронізацію `GET /stock/`.

---

## 7. Як протестувати

**A. Вбудована консоль** — `/dashboard/api/console/` → пресети «🔌 Ping», «🏬 Залишки», «🛒 Перевірка кошика», «📥 Прихід», «🧾 Нове замовлення», «🚚 Відправлення». Ключ підставляється автоматично (перший активний). Замініть `YOUR-SKU` на реальний SKU.

**B. Browsable API** — увійдіть в адмінку і відкрийте `/api/v1/` (тільки читання, при `DJANGO_DEBUG=1`).

**C. curl** (PowerShell: замість `curl` пишіть `curl.exe`):
```bash
curl -H "Authorization: Token $TOKEN" http://localhost:8000/api/v1/ping/
curl -H "Authorization: Token $TOKEN" "http://localhost:8000/api/v1/stock/?in_stock=true"
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
     -d '{"items":[{"sku":"AMP-100","qty":2}]}' http://localhost:8000/api/v1/stock/check/
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
     -d '{"order_number":"TEST-1","client":"Test","addr_country":"DE","lines":[{"sku":"AMP-100","qty":1}]}' \
     http://localhost:8000/api/v1/orders/
```

**D. Python-скрипт** [`api/examples/shop_client.py`](examples/shop_client.py) — готові сценарії магазину: `ping`, `stock`, `check`, `receive`, `order`, `cancel`, `track`, `sync`.

**E. Вебхуки локально:**
```bash
python api/examples/webhook_receiver.py
```
(з `WEBHOOK_SECRET=<секрет вебхука>`), у Minerva вебхук на `http://<IP цього ПК>:9000/` → дія «📡 Надіслати тестову подію».

**F. Автотести:** `python manage.py test api --settings=tabele.settings_test`

---

## 8. Типова схема інтеграції магазину

| Подія в магазині | Виклик Minerva |
|---|---|
| Кожні 5–15 хв (cron) | `GET /stock/?changed_since=<час минулого запуску>` → оновити наявність |
| Користувач у кошику / checkout | `POST /stock/check/` |
| Замовлення оплачене | `POST /orders/` з `check_stock: true` (при помилці мережі — просто повторити) |
| Замовлення скасоване в магазині | `POST /orders/{id}/cancel/` |
| Сторінка «Мої замовлення» | `GET /orders/?order_number=…` + `GET /shipments/?order_number=…` → статус і `tracking_url` |
| Миттєве оновлення наявності / статусів | вебхуки `stock.changed`, `order.status_changed`, `shipment.updated` |

## Коди помилок
| HTTP | `code` | Причина |
|---|---|---|
| 401 | — | немає/невірний/прострочений ключ |
| 403 | — | у ключа немає потрібного scope (назва scope в `detail`) |
| 400 | `invalid_lines`, `fractional_qty`, `location_not_found`, `wrong_location_type`, … | невірні дані |
| 404 | `sku_not_found` | товар не знайдено |
| 409 | `insufficient_stock`, `already_shipped` | бізнес-конфлікт |
| 429 | — | перевищено ліміт запитів |
