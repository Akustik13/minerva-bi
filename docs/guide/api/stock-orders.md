---
title: Сценарії — склад і замовлення з сайту чи CRM
order: 40
summary: Як сайт чи інша система синхронізує залишки, перевіряє кошик, передає замовлення, скасовує їх і показує покупцю статус і трекінг — з прикладами запитів.
where: 🤖 API та скрипти → 📖 Документація API (розділи «Склад», «Замовлення», «Як впровадити в магазин»)
urls: /dashboard/api/docs/, /admin/api/apikey/, /admin/inventory/inventorysettings/
roles: інтегратор, адміністратор
keywords: синхронізація залишків, склад, stock, available, кошик, checkout, замовлення через API, orders, скасувати, трекінг, магазин, WooCommerce, Shopify, PHP, інвентаризація, прихід, Lagerbestand, Bestellung
related: api/overview, api/keys, api/webhooks, api/endpoints, inventory/stock, sales/orders, shop/overview
---

## Що це

Типова інтеграція сайту (чи CRM, чи складської програми) з Minerva — кілька точок:

| Подія у вашій системі | Що викликати в Minerva |
|---|---|
| Змінився залишок у Minerva | вебхук `stock.changed` → оновити наявність ([Вебхуки](guide:api/webhooks)) |
| Раз на 5–15 хв або раз на добу | `GET /stock/?changed_since=<час минулого запуску>` — страховка, якщо вебхук загубився |
| Кошик / checkout | `POST /stock/check/` |
| Замовлення оплачене | `POST /orders/` з `check_stock: true` |
| Замовлення скасоване | `POST /orders/{id}/cancel/` |
| Статус і трекінг для покупця | вебхуки `order.status_changed`, `shipment.updated` або `GET /shipments/?order_number=…` |

Ключ: `stock:read`, `orders:read`, `orders:write`, `shipments:read`, `webhooks:read`, `webhooks:write`;
**«Джерело замовлень»** — напр. `webshop` ([Ключі API](guide:api/keys)).

> ℹ️ Якщо сайт — інтернет-магазин Minerva (каталог і ціни теж з Minerva, номери `PO-…`, кабінет, Stripe),
> дивіться розділ [Інтернет-магазини](guide:shop/overview): там замовлення йдуть з `"shop": true`.

## Підготовка

1. **Артикули.** Артикул товару на сайті має збігатися з артикулом у Minerva. Якщо ні — додайте аліас у
   [аліасах товарів](/admin/inventory/productalias/); API розпізнає його сам.
2. **Склад.** У [⚙️ Налаштування складу](/admin/inventory/inventorysettings/) свідомо виберіть: коли списувати
   товар (при створенні замовлення / при відправці), чи бронювати, чи дозволено від'ємний залишок
   ([Налаштування складу](guide:inventory/settings)).
3. **Ключ** — окремий для сайту, у змінній оточення сервера сайту.

## Залишки

Що означають числа:

| Поле | Значення |
|---|---|
| `on_hand` | фізично на складі |
| `reserved` | обіцяно відкритим замовленням і ще не списано (резерви й замовлення, які спишуться при відправці) |
| `available` | **можна продати** = on_hand − reserved — це показуйте на сайті |
| `incoming` | очікується від постачальників (відкриті закупівлі) |
| `in_stock`, `low_stock` | є в наявності / нижче точки дозамовлення |

Перша синхронізація — усі товари, далі — лише змінені:

```bash
curl -H "Authorization: Token $MINERVA_KEY" "{{base_url}}/api/v1/stock/?page_size=500"
curl -H "Authorization: Token $MINERVA_KEY" "{{base_url}}/api/v1/stock/?changed_since=2026-10-29T08:00:00Z"
curl -H "Authorization: Token $MINERVA_KEY" "{{base_url}}/api/v1/stock/FT220407/"
```

Фільтри: `sku=A,B`, `category`, `search`, `in_stock=true`, `low_stock=true`, `location=MAIN`. Картка одного товару
(`/stock/{sku}/`) додає розбивку по локаціях. Артикул зі «/» — через `?sku=…`.

## Перевірка кошика

Перед оплатою:

```bash
curl -X POST "{{base_url}}/api/v1/stock/check/" \
  -H "Authorization: Token $MINERVA_KEY" -H "Content-Type: application/json" \
  -d '{"items": [{"sku": "FT220407", "qty": 2}, {"sku": "CA-SMA-1M", "qty": 5}]}'
```

Відповідь: `"ok": false`, якщо чогось бракує, і по кожному рядку `requested`, `available`, `ok` — покажіть
покупцю, чого не вистачає.

## Передати замовлення

Після оплати сайт передає замовлення:

```python
import os, requests

BASE = "{{base_url}}/api/v1"
HEAD = {"Authorization": f"Token {os.environ['MINERVA_KEY']}"}

r = requests.post(BASE + "/orders/", headers=HEAD, timeout=30, json={
    "order_number": "WS-1001",            # номер на сайті — обов'язково
    "currency": "EUR",
    "client": "Muster GmbH", "contact_name": "Max Muster",
    "email": "kunde@example.com", "phone": "+49 30 123456",
    "addr_street": "Hauptstr. 1", "addr_zip": "10115", "addr_city": "Berlin", "addr_country": "DE",
    "shipping_cost": 4.90,
    "check_stock": True,                  # не створювати, якщо товару бракує
    "lines": [{"sku": "FT220407", "qty": 2, "unit_price": 12.50}],
})
print(r.status_code, r.json().get("id"), r.json().get("created"))
```

Що робить Minerva:

1. Розпізнає артикули (артикул або аліас). Невідомий → `400 invalid_lines`, замовлення не створюється.
2. `check_stock: true` → `409 insufficient_stock`, якщо товару бракує (перелік — у відповіді).
3. Створює замовлення: склад списується чи бронюється за налаштуваннями складу, клієнт з'являється в CRM,
   приходить сповіщення (email / Telegram). Рядок без ціни — за ціною продажу товару.
4. **Повтор безпечний:** той самий `source` + `order_number` вдруге не створює дубля — відповідь `200` з
   `"created": false` і тим самим замовленням. Тож при збої мережі, 429 чи 5xx сайт просто повторює запит.

Збережіть `id` з відповіді — це номер замовлення в Minerva для подальших запитів.

| Відповідь | Що робити сайту |
|---|---|
| 201 / 200 | зберегти `id` (200 — уже було створене раніше) |
| 409 | повідомити менеджера чи покупця, чого бракує |
| 400 | помилка даних (невідомий артикул) — повідомити менеджера |
| 0 (мережа), 429, 5xx | повторити пізніше з тим самим `order_number` |

## Скасування, статус, трекінг

```bash
# знайти замовлення за номером сайту
curl -H "Authorization: Token $MINERVA_KEY" "{{base_url}}/api/v1/orders/?order_number=WS-1001&source=webshop"
# скасувати (товар повертається на склад; {"restock": false} — не повертати)
curl -X POST -H "Authorization: Token $MINERVA_KEY" "{{base_url}}/api/v1/orders/1203/cancel/"
# трекінг для сторінки «Мої замовлення»
curl -H "Authorization: Token $MINERVA_KEY" "{{base_url}}/api/v1/shipments/?order_number=WS-1001"
```

Статуси замовлення: `received` → `processing` → `shipped` → `delivered`, або `cancelled`. Відправлене чи
доставлене замовлення не скасовується (`409 already_shipped`) — повернення оформлюють вручну. Змінити поля —
`PATCH /orders/{id}/`, напр. `{"status": "processing"}`.

Відправлення: `tracking_number`, `tracking_url` (посилання для покупця), `status`, `carrier_status_label`,
`eta_from` / `eta_to`, `delivered_at`.

## Склад з інших програм

- **Прихід / списання / коригування** — `POST /stock-movements/` (`tx_type`: `Incoming`, `Outgoing`,
  `Adjustment`). Передавайте `external_key` (напр. номер накладної й рядка) — повтор не створить дубля (`200`).
  У відповіді `available_after` — залишок після руху.
- **Інвентаризація** — `POST /stock/count/` з фактичною кількістю: Minerva сама створить коригування на різницю.
  Список рядків обробляється атомарно — помилка в одному скасовує всі.

```json
[{"sku": "FT220407", "qty": 7, "location": "MAIN"}, {"sku": "CA-SMA-1M", "qty": 120}]
```

## Готовий приклад і запуск

- Скрипт **shop_client.py** ([📖 Документація API](/dashboard/api/docs/) → **«⬇ shop_client.py»**): команди
  `ping`, `stock`, `check`, `receive`, `order`, `cancel`, `track`, `sync`. Там же — приклади PHP і Node.js.
- Перевірка без коду — [Консоль API й тестування](guide:api/console).
- Чекліст запуску: усі артикули сайту знаходяться в Minerva; сайт повторює невдалі `POST /orders/`; щоденна повна
  синхронізація залишків увімкнена; перший тиждень хтось дивиться журнал вебхуків і [📜 Журнал API](/admin/api/apirequestlog/).

## Адреси

{{api_endpoints:stock}}

{{api_endpoints:orders}}

{{api_endpoints:shipments}}

## Часті питання та проблеми

**`400 invalid_lines`** — артикул сайту не знайдено в Minerva: виправте артикул або додайте аліас.

**`409 insufficient_stock`** — товару бракує; якщо продаєте під замовлення — передавайте без `check_stock`
(і в налаштуваннях складу дозвольте від'ємний залишок).

**Залишки на сайті розходяться** — масові зміни списком в адмінці подій не надсилають; увімкніть щоденну повну
синхронізацію `GET /stock/`.

**Замовлення з'явилось двічі** — сайт передав різні `order_number` (або різні `source`) для того самого
замовлення. Номер має бути сталим.
