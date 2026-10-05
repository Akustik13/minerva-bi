# Minerva REST API v1

Базовий URL: `https://<ваш-домен>/api/v1/` (локально `http://localhost:8000/api/v1/`)

**Зміст:** 1. Підключення · 2. Склад · 3. Товари · 4. Замовлення · 5. Доставка · 6. Вебхуки · 7. Як протестувати · 8. Як впровадити в магазин · 9. Коди помилок · 10. Безпека і запуск · 11. Інтернет-магазин сайту

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

**0. API-тестер у браузері (найпростіше)** — файл [`examples/api_tester.html`](examples/api_tester.html) (кнопка «🧪 Завантажити API-тестер» на сторінці документації). Відкрийте його подвійним кліком, вставте адресу Minerva і ключ — кнопки «Перевірити підключення», «Залишки», «Перевірка кошика», «Створити тестове замовлення», «Скасувати» тощо самі формують запит і показують відповідь з поясненням коду. Працює з будь-якого комп'ютера: для `/api/v1/` увімкнено CORS (лише ключ у заголовку, cookie не передаються).

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

---

## 9. Коди помилок

У тілі помилки є `detail` (текст для людини) і часто `code` (для коду магазину). Повторювати варто лише `429`, `5xx` і помилки мережі.

| HTTP | `code` | Причина | Що робити |
|---|---|---|---|
| 400 | `invalid_lines` | невідомий SKU або дробова кількість у рядках замовлення | виправити SKU / додати аліас |
| 400 | `fractional_qty`, `zero_qty` | невірна кількість | виправити дані |
| 400 | `location_not_found`, `location_inactive`, `wrong_location_type` | проблема з локацією складу | перевірити `GET /locations/` |
| 401 | — | немає ключа, ключ невірний, неактивний або прострочений | перевірити ключ в адмінці |
| 403 | — | у ключа немає права; назва scope — у `detail` | додати scope до ключа |
| 404 | `sku_not_found` | товар або запис не знайдено | перевірити SKU / id |
| 405 | — | DELETE не підтримується | для замовлень — `/cancel/` |
| 409 | `insufficient_stock` | товару не вистачає; перелік — у `items` / `details` | повідомити покупця або менеджера |
| 409 | `already_shipped` | скасування відправленого замовлення | оформити повернення вручну |
| 429 | — | перевищено ліміт запитів | чекати `Retry-After` секунд і повторити |
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
- [ ] Ліміт запитів підібрано під магазин (`API_RATE_LIMIT`)

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
| `POST /orders/` з `"shop": true` | `orders:write` | лише позиції магазину з ціною; **ціни завжди з Мінерви** (ступінь для кількості; `unit_price`/`total_price` з запиту ігноруються); `check_stock: false` — під замовлення; `payment_status` за замовчуванням `unpaid` |
| `POST /orders/` з `"quote": true` | `orders:write` | **запит пропозиції**: тип документа `QUOTE` («Запит пропозиції»), без цін, `affects_stock: false` (склад не змінюється); невідомі SKU не помилка — записуються в примітку «Поза каталогом Minerva: …» |
| `PATCH /orders/{id}/` | `orders:write` | `{"payment_status": "paid", "payment_reference": "…"}` — після онлайн-оплати |
| `GET /shop/shipping/` | `products:read` | доставка магазину ключа: `configured`, `currency`, `free_shipping{enabled, threshold}`, `allowed_countries` (null — усі), `zones[{name, countries, price, free_shipping, free_from}]`; `countries` може містити `*` — решта світу |

**Акції та новинки.** `ShopListing.discount_percent` + `discount_until` (Sonderangebot) і `new_until` (новинка). У `/shop/products/` `price` і `price_breaks` уже зі знижкою; `offer` = `{percent, until, regular_price, regular_price_breaks}` або `null`; `is_new` — bool. Замовлення з `shop:true` рахуються за ціною зі знижкою.

**Життєвий цикл.** `Product.lifecycle_status` (`active` / `nrnd` / `discontinued`) і `Product.successor` (рекомендована заміна). У `/shop/products/`: `lifecycle_status`, `successor` = `{sku, name}` або `null`. Позиція магазину може перекрити статус і заміну лише для себе (`ShopListing.lifecycle_status` — порожньо = як на складі, `ShopListing.successor`); API віддає вже ефективні значення для магазину ключа. Замовлення з `shop:true` для `discontinued` — лише в межах залишку (інакше 409 `insufficient_stock`).

**Доставка.** Регіони задаються в картці магазину (`ShippingZone`: країни ISO, ціна нетто, участь у безкоштовній доставці) + `Shop.free_shipping_enabled/threshold`. Якщо регіони є, `POST /orders/` з `shop:true` перевіряє `addr_country` (інакше 400 `shipping_not_available`) і сам записує `shipping_cost` (з урахуванням порогу безкоштовної доставки). Регіони можна імпортувати з DigiKey (`GET /offers` → `shippingRates`: країни + мінімальна ціна; порогу в API DigiKey немає).

**Ціни з DigiKey.** `ShopListing.price_source = digikey` + `price_factor` (%): ціна і ступені позиції = ціни офера DigiKey (`bots.DigiKeyListing.dk_prices`) × %, оновлюються сигналом при кожному оновленні цін DigiKey (`pull_dk_listings`, дія «⬇️ Оновити ціни з DigiKey» у товарах — `bots.services.dk_marketplace.refresh_offer_prices`). Ручна зміна ціни/ступенів перемикає позицію на `manual`.

Вебхук `stock.changed` надсилається також, коли змінюється позиція магазину (галочка, ціна, ступені) або назва, категорія, фото товару, що є в магазині, — сайт одразу скидає кеш каталогу.
