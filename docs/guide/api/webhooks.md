---
title: Вебхуки
order: 50
summary: Minerva сама надсилає POST на адресу вашого сайту чи CRM, коли змінився залишок, замовлення, відправлення чи закупівля — з підписом і повторами.
where: 🤖 API та скрипти → 📡 Вебхуки
urls: /admin/api/webhook/, /admin/api/webhook/add/, /admin/api/webhookdelivery/
roles: адміністратор, інтегратор
keywords: вебхук, webhook, подія, stock.changed, order.created, order.status_changed, shipment.updated, purchase_order.updated, підпис, HMAC, секрет, повтор, журнал вебхуків, ping
related: api/overview, api/stock-orders, api/purchases, api/keys, shop/site-admin
---

## Що це

**Вебхук** — адреса вашої системи (сайту, CRM), на яку Minerva надсилає POST щоразу, коли щось змінилось.
Замість того щоб опитувати API кожну хвилину, сайт одразу дізнається: залишок змінився, замовлення відправлено,
з'явився трек-номер.

## Події

| Подія | Коли | Що в `data` |
|---|---|---|
| `stock.changed` | змінився залишок (прихід, продаж, коригування, скасування), а також позиція магазину чи дані товару в магазині | `items` — товари, як у `/stock/`; усі товари однієї зміни — в одній події |
| `order.created` | створено замовлення (з будь-якого джерела) | номер, джерело, статус, дата, сума, валюта… |
| `order.status_changed` | змінився статус замовлення | те саме + `previous_status` |
| `shipment.updated` | з'явився трек-номер або змінився статус відправлення | як у `/shipments/` + `previous_status` |
| `purchase_order.updated` | закупівля: стан, отримано на склад, трекінг, дата, доставка | `purchase_order` і `changed_by` — хто змінив ([Закупівлі з CRM](guide:api/purchases)) |
| `ping` | тест з адмінки або `POST /webhooks/{id}/test/` | `message` |

> ⚠️ `purchase_order.updated` отримують лише вебхуки, які вибрали саме цю подію: «усі події» її не включають,
> щоб дані постачальників і закупівельні ціни не йшли на сайт магазину.

## Як створити вебхук в адмінці

1. [📡 Вебхуки](/admin/api/webhook/) → [Додати вебхук](/admin/api/webhook/add/).
2. **«Загальне»**: **«Назва»** (напр. `shop`), **«URL отримувача»** — HTTPS-адреса обробника на вашому сервері,
   **«Активний»**.
3. **«Що надсилати»**: **«Події»** — галочки (нічого не вибрано = всі події, крім `purchase_order.updated`);
   **«Тільки джерела замовлень»** — напр. `webshop`: події замовлень і відправлень лише цих джерел (порожньо —
   з усіх).
4. **«Зберегти»**. У блоці **«Підпис і стан»** — **«Секрет підпису»** (`whsec_…`, кнопки **«👁 Показати»** /
   **«📋 Копіювати»**) — вставте його в налаштування обробника.
5. У списку вебхуків позначте новий → дія **«📡 Надіслати тестову подію (ping)»** → **«Виконати»**.
   Має з'явитись «shop: доставлено (HTTP 200)».

Сайт магазину Minerva, підключений кодом **«🔗 Підключити сайт»**, отримує вебхук і секрет автоматично
([Адмінка сайту](guide:shop/site-admin)).

## Як створити вебхук через API

Ключ з правами `webhooks:read` і `webhooks:write`; вебхук прив'язується до цього ключа, і ключ бачить лише свої:

```bash
curl -X POST "{{base_url}}/api/v1/webhooks/" \
  -H "Authorization: Token $MINERVA_KEY" -H "Content-Type: application/json" \
  -d '{"name": "shop", "url": "https://shop.example/minerva-hook",
       "events": ["stock.changed", "order.status_changed", "shipment.updated"],
       "order_sources": "webshop"}'
```

У відповіді — `secret` для перевірки підпису. Тест — `POST /webhooks/{id}/test/`, останні 50 доставок —
`GET /webhooks/{id}/deliveries/`.

## Що приходить на ваш сервер

```text
POST https://shop.example/minerva-hook
Content-Type: application/json
X-Minerva-Event: stock.changed
X-Minerva-Delivery: 0b6f…                  ← однаковий при повторах — для відсіву дублів
X-Minerva-Signature: t=1759312345,v1=5d1c…

{"id": "0b6f…", "event": "stock.changed", "created_at": "2026-10-29T12:00:00+00:00",
 "data": {"items": [{"sku": "FT220407", "available": 8}]}}
```

## Перевірка підпису

`v1` = HMAC-SHA256 від рядка `<t>.<сире тіло запиту>` з секретом вебхука, у hex. Відхиляйте запит, якщо підпис не
збігається або `t` старший за 5 хвилин.

```python
import hashlib, hmac, time

def verify(secret: str, body: str, header: str) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(","))
    ts, sig = int(parts["t"]), parts["v1"]
    if abs(time.time() - ts) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
```

```php
$body = file_get_contents('php://input');
parse_str(str_replace(',', '&', $_SERVER['HTTP_X_MINERVA_SIGNATURE'] ?? ''), $sig);
$expected = hash_hmac('sha256', ($sig['t'] ?? '') . '.' . $body, getenv('MINERVA_WEBHOOK_SECRET'));
if (!hash_equals($expected, $sig['v1'] ?? '') || abs(time() - (int)($sig['t'] ?? 0)) > 300) {
    http_response_code(401); exit;
}
```

Важливо: підпис рахується від **сирого** тіла — не розбирайте JSON до перевірки. Готовий приймач для тесту —
**webhook_receiver.py** ([📖 Документація API](/dashboard/api/docs/) → **«⬇ webhook_receiver.py»**).

## Доставка й повтори

- Відповідь `2xx` за 10 секунд = доставлено. Відповідайте одразу, а важку обробку робіть після.
- Інакше Minerva повторює через 1 хв, 5 хв, 30 хв, 2 год, 6 год, 12 год, 24 год, далі — **«Помилка»**.
- Порядок подій не гарантований (через повтори). У `stock.changed` завжди актуальний залишок на момент події —
  просто перезаписуйте його.
- Повтор має той самий `X-Minerva-Delivery` — пропускайте вже оброблені.

У списку [📡 Вебхуки](/admin/api/webhook/) колонка **«Стан»**: «✅ HTTP 200» або «❌ N невдач поспіль (HTTP …)».

[📋 Журнал вебхуків](/admin/api/webhookdelivery/) — кожна доставка: подія, **«Статус»** (Очікує / Доставлено /
Помилка), **«Спроб»**, **«HTTP-код»**, наступна спроба, відповідь вашого сервера. Фільтри — статус, подія,
вебхук. Дія **«🔁 Надіслати повторно зараз»** — не чекати наступної спроби.

## Адреси API вебхуків

{{api_endpoints:webhooks}}

## Часті питання та проблеми

**Тестова подія: «помилка — 401»** — ваш обробник не прийняв підпис: секрет на сервері інший, або підпис
рахується від уже розібраного JSON.

**«помилка — 404 / 500»** — невірна адреса обробника або помилка у вашому коді; текст відповіді — у журналі вебхуків.

**Події не приходять зовсім** — вебхук не **«Активний»**, подію не вибрано, або **«Тільки джерела замовлень»**
не включає джерело цього замовлення.

**Залишки іноді не оновлюються** — масові зміни списком в адмінці подій не надсилають. Раз на добу робіть повну
синхронізацію `GET /stock/` ([Склад і замовлення](guide:api/stock-orders)).
