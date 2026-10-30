# Minerva REST API v1

Базовий URL: `https://<ваш-домен>/api/v1/` (локально `http://localhost:8000/api/v1/`)

**Зміст:** 1. Підключення · 2. Склад · 2a. Закупівлі з CRM · 3. Товари · 4. Замовлення · 5. Доставка · 6. Вебхуки · 7. Як протестувати · 8. Як впровадити в магазин · 8a. Як впровадити в CRM · 9. Коди помилок · 10. Безпека і запуск · 11. Інтернет-магазин сайту

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
| `purchases:read` / `purchases:write` | закупівлі зовнішньої CRM: замовлення постачальникам, трекінг, «доставлено» (розділ 2a) |
| `customers:read` / `customers:write` | клієнти CRM |
| `webhooks:read` / `webhooks:write` | вебхуки, зареєстровані цим ключем |
| `accounts:read` / `accounts:write` | особистий кабінет покупців магазину ключа (розділ 11a) |

**Ліміт:** 120 запитів/хв на ключ (змінна оточення `API_RATE_LIMIT`, напр. `300/min`), або свій у ключа (нижче). При перевищенні — `429` + заголовок `Retry-After`.
**Пагінація:** `?page=2&page_size=200` (макс. 500). Відповідь: `{count, next, previous, results}`.
**Сортування:** `?ordering=-available`, `?ordering=sku`.
**DELETE** не підтримується ніде (405).

### Обмеження ключа (Адмін → REST API → API Ключі → «🛡 Обмеження»)

Кожне поле порожнє — без обмеження. Обмеження діють разом із правами (scopes).

| Поле | Що робить | Відповідь, коли не можна |
|---|---|---|
| Дозволені IP-адреси | лише з цих адрес / мереж (`203.0.113.5`, `10.0.0.0/24`, через кому чи з нового рядка) | `401`, у `detail` — адреса, з якої прийшов запит |
| Запитів за хвилину | свій ліміт замість загального `API_RATE_LIMIT` | `429` + `Retry-After` |
| Запитів за годину / за добу | календарна година / доба (Europe/Berlin); відмова в ліміт не рахується | `429` + `Retry-After`, у `detail` — який ліміт вичерпано |
| Різних записів за добу | скільки різних товарів / замовлень (за `sku`, `id`, `code`, `order_number`) ключ бачить за добу. Уже переглянуті сьогодні — без обмежень. У списках решта приховується: `"quota": {"items_per_day", "used", "hidden"}`, заголовки `X-Items-Per-Day`, `X-Items-Remaining` | один запис понад ліміт — `429`, `code: daily_items_limit` |
| Лише ці поля у відповідях | напр. `sku, available` — у списках і картках записів лишаються тільки ці поля (інші відповіді, як `/ping/`, — без змін) | — |

Приклад «тільки кількість зі складу, не більше 10 товарів на добу, 200 запитів на добу, лише з сервера партнера»: право `stock:read`, поля `sku, available`, різних записів за добу `10`, запитів за добу `200`, дозволена IP — адреса сервера.

**Адреса клієнта** за зворотним проксі DSM — `X-Real-IP`, інакше останній елемент `X-Forwarded-For`, інакше адреса з'єднання.

### Журнал запитів (Адмін → REST API → 📜 Журнал API)

Кожен запит з ключем — і кожна спроба з невідомим ключем — записується: коли, ключ (невідомий — лише початок і кінець), IP-адреса, метод і адреса (секретні параметри як `***`), дія й право, код відповіді та помилки, скільки записів повернуто й усі артикули / номери (до 2000 на запит), тривалість, програма (User-Agent), примітка (напр. «приховано 3: ліміт 10 різних записів за добу», «адреса не в Дозволених IP»). Для змін (POST / PATCH / PUT / DELETE) — назви полів тіла без значень і що відповіла Minerva (номер, статус, рух складу). Тіло запиту не пишеться.

- «Що сталося» — кожен запит одним реченням: «API RAG прочитав список товарів зі складом — 169 записів (сторінка 1, page_size 500)».
- «📊 Аналіз журналу» (`/admin/api/apirequestlog/analysis/`): за ключем і періодом — запити по днях, читання / зміни / відмови, які товари й замовлення ключ отримав, IP, програми, кожна зміна, CSV.
- Картка ключа → «🔍 Що бачить і що може змінити цей ключ»: кожна адреса з правом, полями відповіді й тіла для цього ключа; «👁 Приклад даних» (GET як ключ, без лімітів, лічильників і журналу) і «🧪 Що змінить запит» (перевірка тіла без запису).

- Фільтри: ключ, успішні / помилки / 401 / 403 / 429 / 5xx, читання / зміни, метод, дата; пошук за IP, адресою, артикулом.
- У картці ключа — «Активність»: запити сьогодні й за 7 днів, помилки, IP-адреси з часом останнього звернення, що найчастіше робив, останні 10 запитів; «Використано зараз» — година, доба, різні записи.
- «Журнал запитів» ключа: усі / лише зміни (POST, PATCH, DELETE) і помилки / лише помилки / не записувати. Ключ RAG, що постійно опитує стрічку, — «лише зміни й помилки».
- Зберігається `API_LOG_DAYS` днів (60; змінна оточення), старіше видаляє `manage.py api_log_cleanup` (cron, раз на добу).

---

## 2. Склад

### Що означають цифри

| Поле | Значення |
|---|---|
| `on_hand` | фізично на складі (всі рухи, крім броні) |
| `reserved` | обіцяно відкритим замовленням і ще не списано: резерви (режим «Бронювати замовлення») + позиції незакритих замовлень, які спишуться пізніше (списання при «Відправлено» / «Доставлено»). Уже списані замовлення сюди не входять — їх немає в `on_hand` |
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
  {"sku": "AMP-100", "product_sku": "AMP-100", "found": true, "requested": 2, "requested_total": 2, "available": 8, "ok": true},
  {"sku": "CABLE-M", "product_sku": "CABLE-M", "found": true, "requested": 5.5, "requested_total": 5.5, "available": 3, "ok": false}
]}
```
Той самий товар під різними артикулами (артикул і його аліас) перевіряється разом: `requested_total` — скільки цього товару в кошику всього, `ok` — чи вистачає «Доступно» на всіх.

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

## 2a. Закупівлі з зовнішньої CRM (`/purchase-orders/`)

Сценарій: замовлення постачальнику робить **ваша CRM**, а склад веде Minerva. CRM передає, у кого, що, скільки й коли замовили, номер підтвердження постачальника, трек-номер, орієнтовну доставку — склад бачить товар у колонці «Очікується» (`incoming` у `/stock/`). Коли CRM дізналась, що товар доставлено, вона повідомляє Minerva — та оприбутковує його на склад **один раз**, навіть якщо повідомлення прийде двічі.

Права ключа: `purchases:read` (читання), `purchases:write` (передати, змінити, трекінг, доставка, скасування). Окремий ключ для CRM; у полі ключа «Джерело замовлень» — назва системи, напр. `crm` (порожньо — `crm`). Її видно в Minerva в колонці «Звідки» закупівель.

### Як Minerva не допускає дублікатів

| Що | Ключ | Повтор |
|---|---|---|
| Замовлення | `source` + `external_id` (номер у вашій CRM) | `POST` з тим самим номером **оновлює** те саме замовлення (`200`, `"created": false`) |
| Позиція | `line_id` (номер рядка у CRM), інакше артикул | оновлює кількість / ціну цього рядка; однаковий артикул кількома рядками — з різними `line_id` |
| Відправлення | трек-номер (без регістру) | одне вхідне відправлення на номер |
| Доставка | `delivery_id`, інакше `tracking:<трек-номер>`, інакше «вся решта» | `200` з `"duplicate": true`, на склад нічого не додається |
| Забагато | отримано більше, ніж замовлено | `409 over_receipt` — схоже на ту саму доставку під іншим id; якщо справді так — `"allow_over": true` |

### `POST /purchase-orders/` — передати замовлення
```json
{
  "external_id": "B-2026-0042",
  "supplier": {"name": "Acme Components GmbH", "email": "sales@acme.example", "contact_person": "J. Doe",
               "phone": "+49 30 123", "country": "DE", "currency": "EUR"},
  "supplier_order_no": "AB-7781",
  "order_date": "2026-10-20",
  "expected_date": "2026-11-05",
  "currency": "EUR",
  "notes": "Rahmenvertrag 2026",
  "tracking_number": "1Z999AA10123456784",
  "carrier": "UPS",
  "lines": [
    {"line_id": "1", "sku": "AMP-100", "qty": 10, "unit_price": "12.50", "description": "Amplifier 100 W"},
    {"line_id": "2", "sku": "CABLE-M", "qty": 25}
  ]
}
```
- Постачальник — рядок (назва) або об'єкт; шукається за назвою, потім email, інакше створюється картка постачальника. Заповнені поля наявної картки не перезаписуються.
- Артикул шукається як у `/stock/` (SKU, аліас, без регістру). Немає в Minerva — позиція зберігається без товару, у відповіді `warnings: [{"code": "sku_not_found", …}]`, на склад вона не рахується. `"strict": true` — тоді `404 sku_not_found` і нічого не зберігається.
- Позиції, яких немає в запиті, не змінюються; `qty: 0` — позицію скасовано. Кількість не може стати меншою за вже отриману (`409 qty_below_received`).
- `status`: лише `ordered` або `cancelled`; `partial` / `received` Minerva рахує сама з доставок.
- `tracking_number` у замовленні = те саме, що `POST …/shipments/` (статус не змінюється).

Відповідь `201` (нове) / `200` (оновлено) — закупівля з `"created"` і `"warnings"`:
```json
{
  "id": "PO-57", "code": "PO-57", "source": "crm", "external_id": "B-2026-0042",
  "status": "ordered", "status_label": "Ordered",
  "supplier": {"id": 4, "name": "Acme Components GmbH", "email": "sales@acme.example", "country": "DE", …},
  "supplier_order_no": "AB-7781", "order_date": "2026-10-20", "expected_date": "2026-11-05", "received_date": null,
  "currency": "EUR", "total": 125, "carrier": "UPS", "tracking_number": "1Z999AA10123456784",
  "delivered_reported_at": null, "qty_ordered": 35, "qty_received": 0, "qty_open": 35,
  "lines": [{"line_id": "1", "sku": "AMP-100", "product_sku": "AMP-100", "in_minerva": true,
             "qty_ordered": 10, "qty_received": 0, "qty_open": 10, "unit_price": 12.5, …}],
  "shipments": [{"tracking_number": "1Z999AA10123456784", "carrier": "UPS", "status": "in_transit",
                 "expected_date": null, "shipped_date": null, "received_date": null, …}],
  "deliveries": [],
  "created_at": "…", "updated_at": "…", "created": true, "warnings": []
}
```

### `GET /purchase-orders/` і `GET /purchase-orders/{ref}/`
`{ref}` — код Minerva (`PO-57`) або `ext:<номер у CRM>` (`ext:B-2026-0042`). Номер зі «/» — `GET /purchase-orders/?external_id=2026/42`.
Фільтри списку: `status=ordered,partial`, `open=1` (ще очікуються), `source=crm`, `external_id`, `supplier` (частина назви), `sku`, `tracking_number`, `changed_since=2026-10-29T08:00:00+01:00` (для синхронізації — змінені з цього часу).

### `PATCH /purchase-orders/{ref}/` — змінити
Ті самі поля, що в `POST`, крім `external_id`: нова орієнтовна дата, номер підтвердження постачальника, позиції, `status: "cancelled"`.

### `POST /purchase-orders/{ref}/shipments/` — трек-номер і стан
```json
{"tracking_number": "1Z999AA10123456784", "carrier": "UPS", "tracking_url": "https://…",
 "status": "customs", "carrier_status": "Zollabfertigung", "expected_date": "2026-11-03", "shipped_date": "2026-10-28",
 "events": [{"time": "2026-10-28T09:00:00Z", "text": "Picked up"}]}
```
`status`: `pending`, `in_transit`, `customs`, `arrived`, `exception`, `delivered` (= ще й доставка, як нижче). `expected_date` відправлення стає орієнтовною датою замовлення.

### `POST /purchase-orders/{ref}/deliveries/` — «доставлено»
Уся решта (одне відправлення):
```json
{"tracking_number": "1Z999AA10123456784", "delivered_at": "2026-11-04T10:15:00+01:00"}
```
Частина:
```json
{"delivery_id": "LS-2026-118", "delivered_at": "2026-11-04", "lines": [{"line_id": "1", "qty": 4}, {"sku": "CABLE-M", "qty": 25}]}
```
- З переліком позицій потрібен `delivery_id` (або `tracking_number`), інакше `400 delivery_id_required` — так Minerva відрізняє повтор від нової доставки.
- Без позицій оприбутковується вся решта. Якщо в замовлення кілька незакритих відправлень, а позицій не передано, — відправлення позначається доставленим, на склад нічого не йде, у `warnings` — `lines_needed`.
- Що робить Minerva — «Налаштування складу» → «Зовнішня система повідомила «доставлено»»: **оприбуткувати одразу** (за замовчуванням; прихід — як «Отримано» в огляді складу, якщо ввімкнено «Додавати при надходженні закупівлі») або **лише позначити «прибуло»** (`booked: false`) — тоді товар приймає склад у Minerva.

Відповідь `201` (нова доставка) / `200` (повтор) — закупівля з `"delivery"`:
```json
{"delivery": {"duplicate": false, "delivery_id": "LS-2026-118", "booked": true, "stock_updated": true,
              "delivered_at": "2026-11-04T00:00:00+01:00", "lines": [{"line": 81, "line_id": "1", "sku": "AMP-100", "qty": "4"}],
              "warnings": []},
 "status": "partial", …}
```

### Інше
- `POST /purchase-orders/{ref}/cancel/` `{"reason": "…"}` — скасувати: решта більше не очікується, отримане лишається.
- `POST /purchase-orders/shipments/` і `POST /purchase-orders/deliveries/` — те саме з `"external_id"` у тілі (для номерів зі «/»).
- Закупівлі, створені в Minerva, через API лише читаються (`403 not_external`).
- Вебхук `purchase_order.updated` (розділ 6) — Minerva повідомляє CRM, коли склад прийняв товар, змінився стан чи трекінг.
- Готовий клієнт — [`examples/crm_purchases.py`](examples/crm_purchases.py), план впровадження — розділ 8a.

### Повний цикл — приклад
Замовили в постачальника 10 шт `AMP-100` і 25 шт `CABLE-M`; приїхало двома доставками. Відповіді скорочено.

| # | Подія в CRM | Запит | Відповідь Minerva | Склад |
|---|---|---|---|---|
| 1 | Замовлення постачальнику підтверджено | `POST /purchase-orders/` (як вище) | `201`, `PO-57`, `status: ordered` | очікується: AMP-100 +10, CABLE-M +25 |
| 2 | Тайм-аут — CRM повторює той самий запит | той самий `POST /purchase-orders/` | `200`, `created: false` | без змін |
| 3 | Постачальник надіслав трек-номер | `POST /purchase-orders/ext:B-2026-0042/shipments/` `{"tracking_number": "1Z…", "carrier": "UPS", "expected_date": "2026-11-03"}` | `200`, `expected_date: 2026-11-03` | без змін |
| 4 | Приїхала перша частина (накладна LS-118) | `POST …/ext:B-2026-0042/deliveries/` `{"delivery_id": "LS-118", "lines": [{"line_id": "1", "qty": 4}]}` | `201`, `delivery.booked: true`, `status: partial` | на складі AMP-100 +4, очікується 6 |
| 5 | Перевізник повторно надіслав «доставлено» LS-118 | той самий запит | `200`, `delivery.duplicate: true` | **без змін** |
| 6 | Помилково та сама доставка під іншим номером | `{"delivery_id": "LS-118-B", "lines": [{"line_id": "1", "qty": 9}]}` | `409 over_receipt` (лишилось 6) | без змін |
| 7 | Решта приїхала (накладна LS-131) | `{"delivery_id": "LS-131"}` — без позицій = уся решта | `201`, `status: received` | AMP-100 +6, CABLE-M +25 |

Після кроків 1, 3, 4 і 7 Minerva надсилає вебхук `purchase_order.updated` з `changed_by: "crm"` — CRM його пропускає. Коли склад сам прийняв товар у Minerva («Отримано» в огляді складу), приходить `changed_by: "minerva"` — тоді CRM оновлює у себе кількість отриманого.

Те саме одним скриптом: `python api/examples/crm_purchases.py demo AMP-100` (розділ 7, п. D).

---

## 3. Товари

`GET /products/` — каталог (ціни, вага, HS-код, країна походження, `image_url`, `datasheet_url`).
- `?with_stock=1` — додає поля залишків (як у `/stock/`)
- Ті ж фільтри, що й у `/stock/`
- `POST /products/`, `PATCH /products/{id}/` — scope `products:write`
- `lifecycle_status` — `active` / `nrnd` / `discontinued`

**Технічні атрибути** (стягуються з DigiKey; набір різний для різних товарів і категорій, тому — словник, а не фіксовані поля; порожні значення не віддаються):
- `tech_attributes` — параметри з людськими назвами: `{"Antenna Type": "PCB Trace", "Frequency Range": "2.4GHz ~ 2.485GHz", "Gain": "2dBi, 4dBi", …}`
- `compliance` — службові коди DigiKey: `{"hts": "8529.10.9100", "eccnNumber": "EAR99", "packaging": "BULK", …}`

**Базові ціни** (власні ціни за кількістю з картки товару; можуть бути імпортовані з DigiKey, далі змінюються вручну, кожна зміна — в історії товару):
```json
"base_prices": {
  "currency": "USD",
  "price_breaks": [{"min_qty": 1, "unit_price": 4.89}, {"min_qty": 10, "unit_price": 4.52}, …],
  "source": "DigiKey · офер f11e32f3-… · синхр. 30.06.2026 15:57",
  "updated_at": "2026-10-06T10:12:00+02:00", "updated_by": "admin"
}
```
`source` — звідки ціни («DigiKey · офер … · синхр. …» або «Змінено вручну: <користувач>»); `null` — базових цін немає.

`GET /shop/products/` теж віддає `tech_attributes` (без `compliance` і базових цін — у магазину ціни позиції).

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
6. **Замовлення інтернет-магазину** (`"shop": true`): Minerva дає своє `order_number` — `PO-<Kundennummer>-NNN` (Kundennummer — одна на клієнта CRM, з 31728, `shop/numbering.py`); номер сайту зберігається як `ShopOrder.site_ref`, повтор з ним повертає те саме замовлення. Сайт бере номер з відповіді (листи, оплата); оплата й кабінет приймають і номер сайту.

Відповідь `201`: повне замовлення з `lines`, `shipments` і `"created": true`.

### Інші операції
| Запит | Опис |
|---|---|
| `GET /orders/?order_number=WS-1001&source=webshop` | знайти замовлення за номером магазину |
| `GET /orders/?status=shipped&date_from=2026-10-01` | фільтри: `status`, `source`, `email`, `date_from`, `date_to` |
| `GET /orders/{id}/` | деталі + рядки + відправлення з трекінгом |
| `PATCH /orders/{id}/` | змінити поля, напр. `{"status": "processing"}` |
| `POST /orders/{id}/cancel/` | скасувати; товар повертається на склад так само, як при скасуванні в Minerva (`{"restock": false}` — списаний товар не повертати, резерв знімається завжди; таке списання позначається `so-writeoff:` і «🔄 Синхронізувати склад» чи видалення замовлення його вже не повертають). Повтор нічого не повертає вдруге. Відправлені/доставлені — `409` |

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
| `purchase_order.updated` | закупівля: стан, отримано на склад, трекінг, орієнтовна дата, доставка. **Лише вебхукам, які вибрали саме цю подію** («усі події» її не отримують), підписатись через API — ключ з `purchases:read` | `{"purchase_order": <як у /purchase-orders/>, "changed_by": "crm"}` — `changed_by` = хто змінив: джерело ключа API або `minerva`, щоб CRM не реагувала на власні зміни |
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

**0. API-тестер у браузері (найпростіше)** — файл [`examples/api_tester.html`](examples/api_tester.html) (кнопка «🧪 Завантажити API-тестер» на сторінці документації). Відкрийте його подвійним кліком, вставте адресу Minerva і ключ — кнопки «Перевірити підключення», «Залишки», «Перевірка кошика», «Створити тестове замовлення», «Скасувати» тощо самі формують запит і показують відповідь з поясненням коду. Блок «Закупівлі з CRM»: «Передати замовлення» → «Трек-номер» → «Доставлено» (ще раз — `duplicate`) → «Скасувати». Працює з будь-якого комп'ютера: для `/api/v1/` увімкнено CORS (лише ключ у заголовку, cookie не передаються).

**A. Вбудована консоль** — `/dashboard/api/console/` → пресети «🔌 Ping», «🏬 Залишки», «🛒 Перевірка кошика», «📥 Прихід», «🧾 Нове замовлення», «🚚 Відправлення»; закупівлі CRM — «📦 Закупівля CRM», «🚛 Трек закупівлі», «✅ Доставлено», «📋 Очікуються» (номер `B-TEST-1`). Ключ підставляється автоматично (перший активний). Замініть `YOUR-SKU` на реальний SKU.

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

# Закупівля з CRM: замовлення → трек → доставлено (повтор останнього → "duplicate": true)
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
     -d '{"external_id":"B-TEST-1","supplier":"Acme Components GmbH","lines":[{"line_id":"1","sku":"AMP-100","qty":10}]}' \
     http://localhost:8000/api/v1/purchase-orders/
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
     -d '{"tracking_number":"1Z999AA10123456784","carrier":"UPS","expected_date":"2026-11-05"}' \
     http://localhost:8000/api/v1/purchase-orders/ext:B-TEST-1/shipments/
curl -X POST -H "Authorization: Token $TOKEN" -H "Content-Type: application/json" \
     -d '{"delivery_id":"LS-1","lines":[{"line_id":"1","qty":4}]}' \
     http://localhost:8000/api/v1/purchase-orders/ext:B-TEST-1/deliveries/
```

**D. Python-скрипт** [`api/examples/shop_client.py`](examples/shop_client.py) — готові сценарії магазину: `ping`, `stock`, `check`, `receive`, `order`, `cancel`, `track`, `sync`.

**D2. Python-скрипт для CRM** [`api/examples/crm_purchases.py`](examples/crm_purchases.py) — закупівлі: `order`, `get`, `list`, `track`, `delivered`, `cancel`, `sync`, а `demo <артикул>` проходить увесь цикл з таблиці розділу 2a і показує склад на кожному кроці (для залишків ключу потрібне ще `stock:read`):
```bash
python api/examples/crm_purchases.py demo AMP-100
python api/examples/crm_purchases.py delivered B-2026-0042 LS-118 AMP-100:4
```

**E. Вебхуки локально:**
```bash
python api/examples/webhook_receiver.py
```
(з `WEBHOOK_SECRET=<секрет вебхука>`), у Minerva вебхук на `http://<IP цього ПК>:9000/` → дія «📡 Надіслати тестову подію». Для CRM — подія `purchase_order.updated` і `OWN_SOURCE=crm`: приймач коротко показує стан закупівлі й пропускає зміни самої CRM.

**F. Автотести:** `python manage.py test api --settings=tabele.settings_test`

---

## 8. Як впровадити в магазин

Інтеграція — це чотири точки в серверному коді магазину: синхронізація залишків, перевірка кошика, передача замовлення, прийом вебхуків.

| Подія в магазині | Виклик Minerva |
|---|---|
| Зміна залишку в Minerva | вебхук `stock.changed` → оновити наявність |
| Кожні 5–15 хв або раз на добу (cron) | `GET /stock/?changed_since=<час минулого запуску>` — страховка, якщо вебхук загубився |
| Кошик / checkout | `POST /stock/check/` |
| Замовлення оплачене | `POST /orders/` з `check_stock: true` (при помилці мережі — просто повторити) |
| Замовлення скасоване в магазині | `POST /orders/{id}/cancel/` |
| Статус і трекінг для покупця | вебхуки `order.status_changed`, `shipment.updated`, або `GET /shipments/?order_number=…` |

### План впровадження

1. **SKU.** SKU товару в магазині має збігатися зі SKU в Minerva. Якщо ні — додайте аліас (`/admin/inventory/productalias/`), API розпізнає його сам.
2. **Ключ.** Окремий ключ для магазину: `stock:read`, `orders:read`, `orders:write`, `shipments:read`, `webhooks:read`, `webhooks:write`; джерело замовлень — `webshop`. Ключ — у змінну оточення `MINERVA_TOKEN` на сервері магазину.
3. **Перша синхронізація.** Повний `GET /stock/` → записати `available` у товари магазину. Товари без пари — виправити SKU.
4. **Вебхук.** `POST /webhooks/` (або в адмінці) + обробник із перевіркою підпису. Перевірка: `POST /webhooks/{id}/test/`.
5. **Checkout.** Перед оплатою — `POST /stock/check/`; якщо `ok: false`, показати покупцю, чого бракує.
6. **Замовлення.** Після оплати — `POST /orders/`, зберегти `id` замовлення Minerva. При `0` (мережа), `429`, `5xx` — повторити пізніше з тим самим `order_number`: дубля не буде.
7. **Статуси.** Вебхуки `order.status_changed` і `shipment.updated` оновлюють замовлення в магазині; `tracking_url` — у лист покупцю.
8. **Страховка.** Щоденний cron з повним `GET /stock/`.
9. **Запуск.** Пройти тестовий сценарій і чеклист (розділ 10).

### PHP — клієнт API

```php
<?php
// minerva.php
function minerva(string $method, string $path, ?array $body = null): array {
    $ch = curl_init(getenv('MINERVA_URL') . '/api/v1' . $path);
    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 15,
        CURLOPT_HTTPHEADER     => [
            'Authorization: Token ' . getenv('MINERVA_TOKEN'),
            'Content-Type: application/json',
            'Accept: application/json',
        ],
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body));
    }
    $raw  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);   // 0 = мережа недоступна
    curl_close($ch);
    return [$code, json_decode($raw ?: 'null', true)];
}
```

### PHP — синхронізація залишків (cron)

```php
<?php
require 'minerva.php';
$stateFile = __DIR__ . '/minerva_last_sync.txt';
$since     = @file_get_contents($stateFile) ?: null;   // видаліть файл для повної синхронізації
$started   = gmdate('c');

$path = '/stock/?page_size=500' . ($since ? '&changed_since=' . urlencode($since) : '');
while ($path) {
    [$code, $data] = minerva('GET', $path);
    if ($code !== 200) { exit("Minerva error $code\n"); }
    foreach ($data['results'] as $item) {
        shop_set_stock($item['sku'], $item['available']);   // функція вашого магазину
    }
    $path = $data['next'] ? substr($data['next'], strpos($data['next'], '/stock/')) : null;
}
file_put_contents($stateFile, $started);
```

### PHP — замовлення після оплати

```php
<?php
require 'minerva.php';

function push_order_to_minerva($order): void {   // $order — замовлення вашого магазину
    [$code, $res] = minerva('POST', '/orders/', [
        'order_number'  => (string) $order->number,
        'currency'      => 'EUR',
        'client'        => $order->billing_name,
        'email'         => $order->email,
        'phone'         => $order->phone,
        'addr_street'   => $order->street,
        'addr_city'     => $order->city,
        'addr_zip'      => $order->zip,
        'addr_country'  => $order->country,       // ISO-2: DE, AT, UA...
        'shipping_cost' => $order->shipping_total,
        'check_stock'   => true,
        'lines'         => array_map(fn($i) => [
            'sku' => $i->sku, 'qty' => $i->qty, 'unit_price' => $i->price,
        ], $order->items),
    ]);

    if ($code === 201 || $code === 200) {
        $order->minerva_id = $res['id'];           // 200 = вже було створене раніше
        $order->save();
    } elseif ($code === 409) {
        notify_manager("Немає товару для {$order->number}", $res['items']);
    } elseif ($code === 400) {
        notify_manager("Помилка даних {$order->number}", $res);   // невідомий SKU тощо
    } else {
        queue_retry('push_order_to_minerva', $order->id);   // 0, 429, 5xx — повторити пізніше
    }
}
```

### PHP — прийом вебхука

```php
<?php
// minerva-hook.php — URL цього файлу реєструється в Minerva як вебхук
$body = file_get_contents('php://input');
parse_str(str_replace(',', '&', $_SERVER['HTTP_X_MINERVA_SIGNATURE'] ?? ''), $sig);
$expected = hash_hmac('sha256', ($sig['t'] ?? '') . '.' . $body, getenv('MINERVA_WEBHOOK_SECRET'));
if (!hash_equals($expected, $sig['v1'] ?? '') || abs(time() - (int) ($sig['t'] ?? 0)) > 300) {
    http_response_code(401);
    exit;
}

$delivery = $_SERVER['HTTP_X_MINERVA_DELIVERY'];
if (already_processed($delivery)) { http_response_code(200); exit; }   // повтор

$event = json_decode($body, true);
switch ($event['event']) {
    case 'stock.changed':
        foreach ($event['data']['items'] as $item) {
            shop_set_stock($item['sku'], $item['available']);
        }
        break;
    case 'order.status_changed':
        shop_set_order_status($event['data']['order_number'], $event['data']['status']);
        break;
    case 'shipment.updated':
        shop_set_tracking($event['data']['order_number'],
                          $event['data']['tracking_number'], $event['data']['tracking_url']);
        break;
}
mark_processed($delivery);
http_response_code(200);
```

### Node.js (Express)

```javascript
import crypto from 'node:crypto';
import express from 'express';

const API = `${process.env.MINERVA_URL}/api/v1`;

export async function minerva(method, path, body) {
  const r = await fetch(API + path, {
    method,
    headers: {
      Authorization: `Token ${process.env.MINERVA_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: r.status, data: await r.json().catch(() => null) };
}

// Перевірка кошика
export async function cartAvailable(cart) {
  const { data } = await minerva('POST', '/stock/check/', {
    items: cart.map(i => ({ sku: i.sku, qty: i.qty })),
  });
  return data;   // { ok, items: [{ sku, requested, available, ok }] }
}

// Вебхук: сире тіло потрібне для підпису — тому express.raw, а не express.json
const app = express();
app.post('/minerva-hook', express.raw({ type: 'application/json' }), (req, res) => {
  const body = req.body.toString('utf8');
  const sig = Object.fromEntries(
    (req.get('X-Minerva-Signature') || '').split(',').map(p => p.split('=')));
  const expected = crypto.createHmac('sha256', process.env.MINERVA_WEBHOOK_SECRET)
    .update(`${sig.t}.${body}`).digest('hex');
  const valid = sig.v1?.length === expected.length
    && crypto.timingSafeEqual(Buffer.from(sig.v1), Buffer.from(expected))
    && Math.abs(Date.now() / 1000 - Number(sig.t)) < 300;
  if (!valid) return res.sendStatus(401);

  res.sendStatus(200);                       // відповідаємо одразу, обробляємо після
  const event = JSON.parse(body);
  if (event.event === 'stock.changed') {
    for (const item of event.data.items) setStock(item.sku, item.available);
  }
});
app.listen(3000);
```

## 8a. Як впровадити в CRM (закупівлі)

Інтеграція — чотири виклики з CRM і (за бажанням) один вебхук назад. Довідник запитів — розділ 2a, готовий клієнт — [`examples/crm_purchases.py`](examples/crm_purchases.py).

| Подія в CRM | Виклик Minerva |
|---|---|
| Замовлення постачальнику підтверджено / змінено (кількість, дата, номер AB) | `POST /purchase-orders/` з тим самим `external_id` — створює або оновлює |
| Постачальник надіслав трек-номер, змінився стан у перевізника | `POST /purchase-orders/ext:<номер>/shipments/` |
| Товар доставлено (накладна, сповіщення перевізника) | `POST /purchase-orders/ext:<номер>/deliveries/` з `delivery_id` = номер накладної |
| Замовлення скасовано | `POST /purchase-orders/ext:<номер>/cancel/` |
| Склад прийняв товар у Minerva, змінився стан | вебхук `purchase_order.updated` (`changed_by: "minerva"`) або `GET /purchase-orders/?changed_since=…` |

### План впровадження

1. **Ключ.** Адмін → REST API → API Ключі: назва `crm`, права `purchases:read`, `purchases:write` (для вебхука через API — ще `webhooks:read`, `webhooks:write`; для перевірки залишків — `stock:read`); «Джерело замовлень» — `crm`. Ключ — у налаштуваннях сервера CRM, не в браузері.
2. **Артикули.** Артикул у CRM має збігатися з артикулом Minerva або бути його аліасом (`/admin/inventory/productalias/`). На час запуску — `"strict": true`: невідомий артикул повертає `404 sku_not_found` і нічого не зберігає, тож розбіжності видно одразу.
3. **Номер замовлення.** `external_id` — постійний номер замовлення в CRM (не змінюється при редагуванні). `line_id` — номер рядка в CRM, щоб зміни кількості потрапляли в той самий рядок.
4. **Повтори.** При збої мережі, `429` чи `5xx` CRM повторює **той самий** запит пізніше. Дубля не буде: замовлення — за `external_id`, відправлення — за трек-номером, доставка — за `delivery_id`.
5. **«Доставлено».** Завжди з `delivery_id` (номер накладної / Lieferschein) і, якщо доставка часткова, з позиціями. `409 over_receipt` означає «більше, ніж замовлено» — найчастіше це та сама доставка під іншим номером; оприбутковувати її ще раз не треба.
6. **Що робить склад.** «Налаштування складу» → «Зовнішня система повідомила «доставлено»»: оприбуткувати одразу або лише позначити «прибуло», і тоді товар приймає склад у Minerva.
7. **Зворотний зв'язок.** Вебхук з подією `purchase_order.updated` — лише ця подія, вибрати явно:
   ```json
   POST /api/v1/webhooks/
   {"name": "crm", "url": "https://crm.example/minerva-hook", "events": ["purchase_order.updated"]}
   ```
   Обробник перевіряє підпис (розділ 6) і пропускає події з `changed_by` = вашому джерелу (`crm`) — це зміни самої CRM. Приклад — [`examples/webhook_receiver.py`](examples/webhook_receiver.py) з `OWN_SOURCE=crm`. Страховка — раз на годину чи добу `GET /purchase-orders/?changed_since=<час минулого запуску>` (`crm_purchases.py sync`).
8. **Тест.** `crm_purchases.py demo <артикул>` на тестовому товарі: «очікується» → трек → часткова доставка → повтор (`duplicate`) → «забагато» (`409`) → решта. Потім — Склад → Закупівлі, колонка «Звідки» = `crm`.

---

## 9. Коди помилок

У тілі помилки є `detail` (текст для людини) і часто `code` (для коду магазину). Повторювати варто лише `429`, `5xx` і помилки мережі.

| HTTP | `code` | Причина | Що робити |
|---|---|---|---|
| 400 | `invalid_lines` | невідомий SKU або дробова кількість у рядках замовлення | виправити SKU / додати аліас |
| 400 | `fractional_qty`, `zero_qty` | невірна кількість | виправити дані |
| 400 | `location_not_found`, `location_inactive`, `wrong_location_type` | проблема з локацією складу | перевірити `GET /locations/` |
| 401 | — | немає ключа, ключ невірний, неактивний, прострочений або запит не з «Дозволених IP-адрес» ключа | перевірити ключ / адресу в адмінці |
| 403 | — | у ключа немає права; назва scope — у `detail` | додати scope до ключа |
| 404 | `sku_not_found` | товар або запис не знайдено | перевірити SKU / id |
| 405 | — | DELETE не підтримується | для замовлень — `/cancel/` |
| 409 | `insufficient_stock` | товару не вистачає; перелік — у `items` / `details` | повідомити покупця або менеджера |
| 409 | `already_shipped` | скасування відправленого замовлення | оформити повернення вручну |
| 400 | `external_id_required`, `supplier_required`, `duplicate_line`, `delivery_id_required`, `bad_date`, `bad_qty`, `bad_status` | закупівлі (розділ 2a): чого бракує — у `detail` | виправити запит |
| 403 | `not_external` | закупівля створена в Minerva — через API лише читання | — |
| 409 | `over_receipt`, `qty_below_received` | доставка більша за замовлене / кількість менша за отриману | перевірити, чи це не повтор; інакше `allow_over: true` |
| 429 | — | перевищено ліміт запитів (хвилина / година / доба — у `detail`) | чекати `Retry-After` секунд і повторити |
| 429 | `daily_items_limit` | ліміт «різних записів за добу» ключа вичерпано | уже переглянуті сьогодні записи доступні; нові — завтра |
| 502 | — | тестовий вебхук не доставлено | дивитись `response_code`, `response_body` |
| 5xx | — | помилка сервера Minerva | повторити пізніше; замовлення не дублюються |

---

## 10. Безпека і чекліст запуску

**Тестовий сценарій (~15 хв)**

- [ ] Створити ключ `webshop-test` з усіма scopes і джерелом замовлень `webshop-test`
- [ ] Консоль → «Ping»: у відповіді назва ключа і права
- [ ] «Залишки»: запам'ятати `available` тестового товару
- [ ] «Прихід» на 10 шт: `available_after` +10; повтор з тим самим `external_key` → `200`, залишок не змінився
- [ ] «Перевірка кошика» з qty більше залишку → `ok: false`
- [ ] «Нове замовлення» → `201`, `created: true`; замовлення в адмінці, клієнт у CRM, залишок менший
- [ ] Те саме замовлення ще раз → `200`, `created: false`
- [ ] Замовлення з неіснуючим SKU → `400 invalid_lines`
- [ ] `POST /orders/{id}/cancel/` → `cancelled`, залишок повернувся
- [ ] Приймач вебхуків + вебхук → прихід → подія `stock.changed` у приймачі
- [ ] Вимкнути приймач, зробити прихід → у «Журналі вебхуків» «Очікує»; увімкнути → за 1–5 хв «Доставлено»
- [ ] Прибрати тестові замовлення, вимкнути тестовий ключ і вебхук

**Безпека**

- [ ] Окремий ключ на кожну систему (магазин, тест, скрипти)
- [ ] Лише потрібні scopes; магазину зазвичай не потрібні `stock:write`, `products:write`, `customers:*`
- [ ] Ключ і секрет вебхука — у змінних оточення магазину, не в коді, не в git, не в JavaScript сторінки
- [ ] «Дійсний до» для тимчасових ключів
- [ ] Вебхук — на HTTPS URL; обробник відхиляє запити без валідного підпису
- [ ] Ліміт запитів підібрано під магазин (`API_RATE_LIMIT` або свій у ключа); для партнерів — обмеження ключа (IP, година / доба, поля)
- [ ] Перший тиждень — «📜 Журнал API»: хто, звідки й що робить кожним ключем

**Запуск магазину**

- [ ] Усі SKU магазину знаходяться в Minerva
- [ ] Налаштування складу обрано свідомо: коли списувати, чи бронювати, чи дозволяти від'ємний залишок
- [ ] Магазин повторює невдалі `POST /orders/`
- [ ] Щоденна повна синхронізація залишків увімкнена
- [ ] Перший тиждень хтось дивиться «Журнал вебхуків» і стан вебхука

---

## 11. Інтернет-магазини (сайт sevskiy.de та інші)

Магазин сайту бере товари й ціни тільки з Мінерви і створює в ній замовлення. Налаштування і код сайту — у проєкті сайту, файл `SHOP-ANLEITUNG.md`.

**Кілька магазинів.** Кожен магазин (`/admin/shop/shop/`) має код — він же «Джерело» замовлень (напр. `webshop`), власний асортимент і власні ціни. Один магазин — «за замовчуванням».
- Ключ API → поле **«Магазин»**. Якщо не задано: магазин з кодом = «Джерело замовлень» ключа, інакше магазин за замовчуванням.
- Замовлення з `shop: true` / `quote: true` завжди отримують `source` = код магазину (значення `source` із запиту ігнорується) — у Продажах видно, звідки прийшло. Код магазину автоматично додається в довідник «Джерела замовлень».

**У Мінерві (`/admin/shop/`):**
- «💶 Асортимент і ціни» — позиція = магазин + товар: галочка «Показувати» і ціна (нетто, 1 шт.; порожньо — «Ціна продажу» товару; без ціни — «ціна за запитом») редагуються прямо в списку; маржа, ступені цін, залишок; фільтри «Магазин», «Ціна», «Наявність», «Категорія».
- Масові дії: показати / сховати, одна ціна для обраних, ± %, ціна = закупівля + націнка, ступені цін (шаблон або свої), видалити ступені, ціна = ціна продажу, округлити, **копіювати в інший магазин** (з коефіцієнтом).
- «📦 Каталог → додати в магазин» — усі товари складу з бейджами магазинів; дія «➕ Додати в магазин…».
- «Налаштування цін» — шаблон ступенів (знижки за кількістю, як на DigiKey: 10 / 25 / 100 / 250 / 500 / 1000), націнка за замовчуванням, округлення (0,01 / 0,05 / ,90 / ,99 …).
- Картка товару на складі показує, в яких магазинах він є і за якою ціною.
- Замовлення: поля **«Спосіб оплати»** (Rechnung / Vorkasse / PayPal / Stripe), **«Статус оплати»**, **«Референс оплати»**.

**API:**

| Запит | Scope | Що робить |
|---|---|---|
| `GET /shop/products/` | `products:read` | видимі позиції магазину ключа (без ціни теж, `price: null`); `price_breaks: [{min_qty, unit_price}]`; `sku, name, name_export, category, unit_type, price` (нетто), `available, in_stock, incoming, lead_time_days`, фото, даташит. Закупівельних цін немає. Фільтри як у `/stock/` |
| `GET /shop/products/{sku}/` | `products:read` | один товар магазину (404, якщо його нема в магазині) |
| `GET /shop/media/{sha256}/` | `products:read` | перевірена копія фото / даташита (з поля `media` товару); лише товари, видимі в магазині ключа, і лише коли ввімкнено в Minerva; заголовок `X-Content-SHA256` |
| `POST /shop/media/status/` | `products:read` | звіт сайту `{have: [sha256…], failed: {sha256: текст}, sync_url}` — що вже збережено на сайті (видно на сторінці «🖼 Фото й даташити»); `sync_url` приймається лише на домені «Адреса сайту» магазину → `{ok, wanted, have, sync_url_accepted}` |
| `POST /orders/` з `"shop": true` | `orders:write` | лише позиції магазину з ціною; **ціни завжди з Мінерви** (ступінь для кількості; `unit_price`/`total_price` з запиту ігноруються); `check_stock: false` — під замовлення; `payment_status` за замовчуванням `unpaid` |
| `POST /orders/` з `"quote": true` | `orders:write` | **запит пропозиції**: тип документа `QUOTE` («Запит пропозиції»), без цін, `affects_stock: false` (склад не змінюється); невідомі SKU не помилка — записуються в примітку «Поза каталогом Minerva: …» |
| `PATCH /orders/{id}/` | `orders:write` | `{"payment_status": "paid", "payment_reference": "…"}` — після онлайн-оплати |
| `GET /shop/shipping/` | `products:read` | доставка магазину ключа: `configured`, `currency`, `free_shipping{enabled, threshold}`, `allowed_countries` (null — усі), `zones[{name, countries, price, free_shipping, free_from, delivery_days{min, max}, overrides{CC: {price, delivery_days}}}]`; `countries` може містити `*` — решта світу; `delivery_days` — робочі дні після відправки (null — не задано); `overrides` — винятки окремих країн зони (`price` null — ціна зони) |
| `GET /shop/assistant/` | `products:read` | помічник Minerva (чат RAG) на сайті магазину ключа: `managed` (false — сайт бере все з `private/minerva-config.php`), `enabled`, `upstream_url` ("" — з файлу сайту), `bridge_key` (лише ключу, прив'язаному до магазину; "" — з файлу сайту), `backend` (`bridge` — веб-міст RAG; `rag_api` — RAG API v1 як у «Помічник по продукції»: тоді `upstream_url` і `api_key` — з налаштувань помічника Minerva, `latest`), `remove_file` (сайт видаляє свій `private/minerva-config.php`). Параметри запиту від сайту: `site`, `file`, `file_enabled`, `file_key`, `file_url` — що у файлі сайту (без ключа), видно в картці магазину. Налаштування — картка магазину, блок «🤖 Помічник Minerva на сайті» |
| `POST /shop/staff/login/` | `products:read` (ключ, прив'язаний до магазину) | вхід в адмінку сайту `{login, password, site}` → `{ok, username, name, email, role, role_label, permissions}`; 401 `invalid`, 429 `locked` (5 невдач / 15 хв). Доступ — картка магазину «👥 Доступ до адмінки сайту» |
| `POST /shop/site-events/` | `products:read` (ключ магазину) | журнал адмінки сайту `{user, action, fields[], site}` — лише назви полів, без значень |
| `POST /shop/pair/` | без ключа (10/год з адреси) | код підключення `{code, site_url}` → `{api_key, webhook_secret, shop, shop_name, minerva_url, site_url}`; код одноразовий, 30 хв, створюється в картці магазину «🔗 Підключити сайт» |
| `GET /shop/search/assist/` | `products:read` (ключ, прив'язаний до магазину) | підказки в пошуку сайту: `?config=1` → `{mode: off\|rules\|ai\|rag\|ai_rag}`; `?q=&n=&skus=&lang=&ai=0\|1&log=0\|1` → `{mode, together[{sku, orders}], ai{status: ok\|off\|short\|limit\|unavailable, tips[{sku, reason}], cached}}`; `&rag=1&poll=1` → лише `rag{status: pending\|busy\|ok\|off\|short\|limit\|unavailable, tips[{sku, reason}], note, cached}` — підказки нашого RAG за даташитами (сайт опитує, поки pending/busy). `n` — скільки знайшов сайт (журнал «що шукають»), `skus` — перші результати |
| `GET /shop/site-mail/` | `products:read` (ключ, прив'язаний до магазину) | пошта сайту з Minerva: `{shop, managed}`; якщо в картці «Сайт надсилає листи з цієї ж скриньки» і вибрано «Відправник» — ще `smtp{host, port, security ssl/tls/'', user, password}`, `from`, `from_name`, `reply_to`, `bcc[]` (прихована копія листів покупцям), `notify[]` (сповіщення про нові замовлення), `confirmation` (підтвердження покупцю). Параметри `mail_site`, `mail_source`, `mail_host`, `mail_user`, `mail_from`, `mail_password=0/1`, `mail_notify`, `mail_confirmation`, `mail_recipient` — звіт сайту про власну пошту (без пароля), таблиця «Пошта сайтів» у картці |

**Акції та новинки.** `ShopListing.discount_percent` + `discount_until` (Sonderangebot) і `new_until` (новинка). У `/shop/products/` `price` і `price_breaks` уже зі знижкою; `offer` = `{percent, until, regular_price, regular_price_breaks}` або `null`; `is_new` — bool. Замовлення з `shop:true` рахуються за ціною зі знижкою.

**Життєвий цикл.** `Product.lifecycle_status` (`active` / `nrnd` / `discontinued`) і `Product.successor` (рекомендована заміна). У `/shop/products/`: `lifecycle_status`, `successor` = `{sku, name}` або `null`. Позиція магазину може перекрити статус і заміну лише для себе (`ShopListing.lifecycle_status` — порожньо = як на складі, `ShopListing.successor`); API віддає вже ефективні значення для магазину ключа. Замовлення з `shop:true` для `discontinued` — лише в межах залишку (інакше 409 `insufficient_stock`).

**Доставка.** Зони задаються в картці магазину (`ShippingZone`: країни ISO, ціна нетто, термін `delivery_days_min/max`, участь у безкоштовній доставці, винятки країн `overrides` — власна ціна/термін) + `Shop.free_shipping_enabled/threshold`. Правило для країни — `shop/shipping.rule_for` (зона + виняток країни). Якщо зони є, `POST /orders/` з `shop:true` перевіряє `addr_country` (інакше 400 `shipping_not_available`) і сам записує `shipping_cost` (з урахуванням винятку країни і порогу безкоштовної доставки). Зони можна імпортувати з DigiKey (`GET /offers` → `shippingRates`: країни + мінімальна ціна; порогу й терміну в API DigiKey немає).

**Базові ціни в магазині.** `ShopListing.price_source = base` + `price_factor` (%): ціна і ступені позиції = базові ціни товару (`Product.base_prices`) × курс (`ShopSettings.fx_rates`, якщо валюта товару інша) × %, оновлюються сигналом при зміні базових цін або курсу. Ручна зміна ціни/ступенів перемикає позицію на `manual`. Історія змін базових цін — `inventory.ProductPriceHistory`; імпорт з DigiKey — `inventory/services/base_prices.import_from_digikey`.

**Фото й даташити.** У `/shop/products/` поле `media` = `{image, datasheet}`, кожне `{sha256, size, content_type, ext, source: url|file, stale, updated_at, url}` або `null` (у Minerva немає придатного — сайт показує свій файл); `media: null` — вимкнено на сторінці «🖼 Фото й даташити». Порядок джерел: посилання з картки товару (`image_url` / `datasheet_url`) → файл у картці (`image` / `datasheet_file`). Тип перевіряється за вмістом (JPG / PNG / WebP / GIF, PDF; HTML-сторінка помилки не приймається). Джерело тимчасово недоступне, а посилання й файл не змінювались — `stale: true`, віддається остання перевірена копія. Сайт забирає копії за `url` (адреса = SHA-256, вміст не змінюється), зберігає в себе і звітує `POST /shop/media/status/`. Логіка — `shop/media.py`.

Вебхук `stock.changed` надсилається також, коли змінюється позиція магазину (галочка, ціна, ступені) або назва, категорія, фото товару, що є в магазині, — сайт одразу скидає кеш каталогу.

## 11a. Особистий кабінет покупців (`/shop/account/…`)

Акаунти покупців живуть **лише в Minerva** (`shop.ShopCustomer`, адмінка «👤 Акаунти покупців»); логіка — `shop/accounts.py`, API — `api/accounts.py`, листи — `shop/mail.py`. Сайт — посередник: ключ API тільки на його сервері, покупець отримує від сайту cookie (HttpOnly) з ключем сесії, сайт передає його заголовком **`X-Customer-Session`**. Пароль — хеш Django; ключі сесій і посилань з листів у БД — лише SHA-256.

Увімкнення: магазин → «👤 Особистий кабінет покупців» (+ відправник листів: акаунт пошти або SMTP сповіщень), ключу — `accounts:read` + `accounts:write`. «Адреса сайту» магазину обмежує домен посилань у листах.

| Запит | Що робить |
|---|---|
| `GET shop/account/?link_de=…&link_en=…` | `{enabled, password_min_length}` — чи ввімкнено кабінет (сайт показує / ховає вхід); адреси сторінки кабінету запам'ятовуються для листів з адмінки |
| `POST shop/account/register/` | `{email, password, company, name, phone?, vat_id?, street?, zip?, city?, country?, confirm_b2b, lang, link, claim_order?}` → **202** завжди однаково; лист з `link#verify=<token>` (48 год). На вже підтверджений email — лист «у вас уже є акаунт», пароль не змінюється. Непідтверджену реєстрацію перезаписує нова. `claim_order` — номер замовлення без акаунта (сайт передає його лише з підписаним cookie браузера, що оформив замовлення): після підтвердження email воно переходить у кабінет (не старше 14 днів, лише без акаунта) |
| `POST shop/account/resend/` | `{email, link, lang}` → 202 (повторний лист, не частіше 1/хв) |
| `POST shop/account/verify/` | `{token, password, remember}` → сесія. **Пароль обов'язковий** — посилання з листа без пароля не дає входу |
| `POST shop/account/login/` | `{email, password, remember}` → `{session, expires_at, remember, customer}`; 401 `invalid_credentials`, 403 `email_not_verified` / `account_disabled`, 429 `locked` (+`Retry-After`; 5 невдалих → 15 хв) |
| `POST shop/account/logout/` | завершує сесію з заголовка |
| `POST shop/account/password/forgot/` | `{email, link, lang}` → 202 завжди; лист з `link#reset=<token>` (60 хв, одноразово) |
| `POST shop/account/password/reset/` | `{token, password, remember}` → сесія; усі інші сесії завершуються, email вважається підтвердженим |
| `GET / PATCH / DELETE shop/account/me/` | профіль (`company, name, phone, vat_id, address{…}, shipping{separate, …}, lang, customer_number` (Kundennummer), `payment_method` (за замовчуванням у касі), `orders_count, cart_count`); `PATCH` — і окремі поля («Als Standard speichern»); `DELETE {password}` — видалити акаунт (замовлення лишаються) |
| `POST shop/account/me/password/` | `{current_password, new_password}`; інші сесії завершуються |
| `GET shop/account/me/orders/` | `{count, results[{number, kind: order\|quote, date, status, payment_method, payment_status, currency, net, shipping, vat_rate, vat, gross, items, tracking[{carrier, number, url, status}]}]}` |
| `GET shop/account/me/orders/{number}/` | + `lines[{sku, name, name_en, qty, unit_price, total}]`, `shipping_address`, `billing`, `dates{received, shipped, delivered}`, `offer` (надісланий Angebot для запиту), `documents[{kind: OC\|PI\|CI\|PL, title, name, number, size, date}]` (PDF з теки замовлення, які створила Minerva), `doc_numbers` |
| `GET shop/account/me/orders/{number}/documents/{name}/` | PDF документа зі списку `documents` (інші файли теки — 404) |
| `GET / PUT shop/account/me/cart/` | `{items: {sku: qty}}` (ключі `rq:<артикул>` — запит ціни), до 100 позицій |

Акаунт може створити й менеджер (адмінка «➕ Додати»): без пароля, лист-запрошення з `link#reset=<token>` (7 днів, `accounts.send_invite`); встановлення пароля підтверджує email.

Сесія: без «запам'ятати» — 12 год від останньої дії, з ним — 30 днів (максимум 90). Помилки: `{detail, code, fields?, problems?}`; коди пароля: `password_too_short / numeric / common / like_email`. Акаунти одного магазину не бачать інший магазин.

**`POST /orders/`** (shop / quote) додатково приймає `customer_session` (або заголовок `X-Customer-Session`), `vat_rate`, `billing{…}`, `lang` — замовлення з'являється в кабінеті (`shop.ShopOrder`), відповідь містить `account_linked`. Без сесії замовлення прив'язується до **підтвердженого** акаунта з тим самим email; при підтвердженні email попередні замовлення магазину з цією адресою потрапляють у кабінет. Прибирання: `manage.py shop_accounts_cleanup` (cron щодоби).

## 11b. Онлайн-оплата Stripe

Логіка — `shop/payments.py` (Stripe REST через `requests`, без бібліотеки), API — `api/payments.py`, адмінка — `shop/admin_payments.py`, модель `shop.ShopPayment` (спроба оплати: Checkout Session, PaymentIntent, сума, статус). Ключі Stripe — лише в Minerva: картка магазину → «💳 Оплата Stripe» (`Shop.stripe_enabled`, `stripe_secret_key`, `stripe_webhook_secret`).

| Запит | Scope | Що робить |
|---|---|---|
| `GET shop/payments/` | `orders:read` | `{methods: [prepayment, invoice, stripe] \| null, stripe: {enabled, connected, mode: test\|live}}` — способи оплати каси в порядку магазину (перший — за замовчуванням; `null` — сайт бере свої), `enabled` — Stripe у касі, `connected` — ключ є (давніші замовлення можна оплатити) |
| `POST shop/payments/stripe/checkout/` | `orders:write` | `{order_number, success_url, cancel_url, lang}` → `{url, session_id, amount, currency, expires_at, mode}`. Сума = нетто + доставка + ПДВ (`ShopOrder.vat_rate`; без ставки — 409 `vat_unknown`). Відкрита сесія з тією ж сумою використовується повторно; стару з іншою сумою Minerva закриває (`expire`). Адреси повернення — лише домен «Адреса сайту». Помилки: `already_paid`, `not_payable`, `stripe_disabled`, `stripe_error` |
| `POST shop/payments/stripe/sync/` | `orders:write` | `{order_number}` → `{payment_status, paid}` — Minerva сама питає Stripe (якщо вебхук ще не дійшов) |
| `POST shop/account/me/orders/<number>/pay/` | `accounts:write` | те саме для власного замовлення покупця (заголовок `X-Customer-Session`) |
| `POST payments/stripe/webhook/<код магазину>/` | — | для Stripe: підпис `Stripe-Signature` (HMAC, 5 хв). Події: `checkout.session.completed` / `async_payment_succeeded` → `payment_status = paid`, `payment_reference` = PaymentIntent; `async_payment_failed` → `failed`; `expired`; `charge.refunded` (повне) → `refunded` |

**Перевірка незавершених оплат** (сесія відкрита або «обробляється»): кнопки «💳 Перевірити оплати» в картці клієнта CRM (і дія в списку клієнтів), «🔄 Перевірити оплати клієнта» в акаунті покупця, «🔄 Перевірити незавершені оплати» у «💳 Онлайн-оплати» та в картці магазину. **Автоперевірка** — `manage.py check_stripe_payments` (cron щохвилини): поки від Stripe не було підписаного вебхука за 7 днів (`Shop.stripe_webhook_last_at`), раз на `Shop.stripe_poll_minutes` хв (0 — вимкнено) Minerva питає Stripe про незавершені оплати за 14 днів; щойно вебхуки приходять — автоперевірка зупиняється. `--now` — перевірити все зараз.

**Тексти листів** покупцям і сповіщення команді — шаблони `shop.ShopMailTemplate` (адмінка «✉️ Шаблони листів», shop 0014): магазин × лист × мова (de/en), поля «Тема / Звертання / Текст / Кнопка / Завершення» з підстановками `{shop}`, `{name}`, `{number}`, `{amount}` …; немає активного шаблону — стандартний текст (`shop/mail.py`, `TEXTS`). `{shop}` = `Shop.public_name` («Назва для покупців», також у назві оплати на сторінці Stripe); мова сповіщень команді — `Shop.team_lang`.

Способи оплати магазину — `Shop.payment_methods` (адмінка «💳 Способи оплати на сайті», shop 0013); `POST /orders/` з `shop: true` і вимкненим способом → 400 `payment_method_not_allowed`. Після оплати Minerva надсилає покупцю лист «Zahlung erhalten» (`ShopPayment.notified_at`, один раз) і сповіщення команді (NotificationSettings: email / Telegram). Замовлення з сесією покупця очищає кошик акаунта.

Замовлення сайту з оплатою Stripe створюється з `payment_method = stripe`, `payment_status = pending`; у кабінеті покупця `payment_open = true`, доки оплату не завершено.

## 12. Зовнішня RAG-система (читання каталогу і складу)

RAG читає Minerva під час запиту (без індексації товарів у вектори). Ключ: лише `products:read` і `stock:read`.

- `GET /shop/products/` — товари магазину ключа: `price`, `price_breaks`, `price_origin`, `offer`, `is_new`,
  `lifecycle_status`, `successor`, `base_prices`, `tech_attributes` (технічні параметри `{назва: значення}`, порожні пропускаються).
  Ціна: своя ціна позиції → «Ціна продажу» товару → базові ціни товару за курсом (`price_origin`: `listing` / `sale_price` /
  `base`; `null` — «за запитом»). `base_prices` = `{currency, price_breaks}` — базові ціни у своїй валюті (напр. USD), навіть
  якщо курсу немає. Закупівельних цін і
  приватних полів немає. Не використовуйте для RAG `/products/` — там закупівельна ціна і нотатки.
- `GET /stock/` — `available` (доступно для продажу), `incoming` (очікується).
- Звуження на боці Minerva: `?sku=A,B`, `?search=…` (SKU, назва, виробник), `?category=…`,
  `?attr=Назва:значення` (входження без регістру; кілька `attr` — усі умови), напр.
  `/shop/products/?attr=RF Family/Standard:Bluetooth&attr=Antenna Type:PCB`.
