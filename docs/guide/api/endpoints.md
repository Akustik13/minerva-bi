---
title: Усі адреси API з прикладами
order: 30
summary: Повний список адрес REST API Minerva прямо з коду — метод, потрібне право ключа, приклади curl і Python для кожної.
where: 🤖 API та скрипти → 📖 Документація API
urls: /dashboard/api/docs/, /dashboard/api/console/
roles: інтегратор, адміністратор
keywords: адреси API, endpoints, ендпоінти, маршрути, список API, curl, python, приклад запиту, stock, orders, products, shipments, customers, webhooks, purchase-orders
related: api/overview, api/keys, api/stock-orders, api/purchases, api/webhooks
---

## Як читати список

Кожен рядок — метод (**GET** читає, **POST** створює чи виконує дію, **PATCH** змінює поля, **PUT** замінює
цілком, **DELETE** видаляє), адреса й коротко, що робить. Праворуч **🔑** — право, яке потрібне ключу
([Ключі API](guide:api/keys)). Клік по рядку — приклади **curl** і **Python** з адресою цього сервера; **📋** —
скопіювати. У прикладах `$MINERVA_KEY` / `KEY` — ваш ключ.

`{pk}` — номер запису в Minerva (поле `id` у відповіді), `{sku}` — артикул, `{ref}` — номер закупівлі (`PO-57`
або `ext:<номер у CRM>`). Список будується з коду Minerva: нова адреса API з'являється тут сама.

> ℹ️ Тіло POST / PATCH у прикладах — заготовка з полів; що саме передавати в типових сценаріях — на сторінках
> [Склад і замовлення](guide:api/stock-orders), [Закупівлі з CRM](guide:api/purchases), [Вебхуки](guide:api/webhooks).
> DELETE для товарів, замовлень і клієнтів відповідає 405 — замовлення скасовується через `…/cancel/`.

## Перевірка підключення

Без окремого права — будь-який дійсний ключ. Повертає назву ключа і його права.

{{api_endpoints:ping}}

## Склад

Залишки (`on_hand` — фізично, `reserved` — заброньовано, `available` — можна продати, `incoming` — очікується),
перевірка кошика, прихід / списання / коригування, інвентаризація, журнал рухів.

{{api_endpoints:stock}}

Локації складу (`MAIN` тощо) — для рухів і залишків по одній локації:

{{api_endpoints:locations}}

## Товари й категорії

Каталог з цінами, вагою, HS-кодом, технічними параметрами (`tech_attributes`) і базовими цінами. Для RAG і
сайтів краще `/shop/products/` — там немає закупівельних цін.

{{api_endpoints:products}}

{{api_endpoints:categories}}

## Замовлення

Створення (ідемпотентно за джерелом і номером), пошук за номером магазину, зміна статусу, скасування з поверненням
товару на склад.

{{api_endpoints:orders}}

## Відправлення

Відправлення замовлень: перевізник, трек-номер, посилання для покупця, статус, очікувана дата.

{{api_endpoints:shipments}}

## Клієнти

Клієнти CRM.

{{api_endpoints:customers}}

## Закупівлі зовнішньої CRM

Замовлення постачальникам, трек-номери, «доставлено», скасування — [Закупівлі з CRM](guide:api/purchases).

{{api_endpoints:purchase-orders}}

## Вебхуки

Підписки цього ключа на події Minerva, тест і журнал доставок — [Вебхуки](guide:api/webhooks).

{{api_endpoints:webhooks}}

## Інтернет-магазин і кабінет покупця

Для сайту магазину Minerva (ключ, прив'язаний до магазину). Що як працює — у розділі
[Інтернет-магазини](guide:shop/overview).

Товари магазину з цінами й наявністю ([Асортимент і ціни](guide:shop/catalog-prices)):

{{api_endpoints:shop/products}}

Доставка — регіони, ціни, безкоштовна доставка ([Доставка](guide:shop/shipping-zones)):

{{api_endpoints:shop/shipping}}

Кабінет покупця — реєстрація, вхід, профіль, замовлення, кошик ([Кабінет покупця](guide:shop/customer-accounts)):

{{api_endpoints:shop/account}}

Онлайн-оплата ([Оплата](guide:shop/payments)); адресу вебхука Stripe викликає сам Stripe, без ключа:

{{api_endpoints:shop/payments}}

{{api_endpoints:payments}}

Фото й даташити для сайту ([Фото й даташити для сайту](guide:shop/media)):

{{api_endpoints:shop/media}}

Помічник, підказки в пошуку й пошта сайту ([Помічник і пошук](guide:shop/assistant-search),
[Пошта магазину й сайту](guide:shop/mail)):

{{api_endpoints:shop/assistant}}

{{api_endpoints:shop/search}}

{{api_endpoints:shop/site-mail}}

Адмінка сайту: вхід співробітників, журнал, підключення сайту кодом ([Адмінка сайту](guide:shop/site-admin)):

{{api_endpoints:shop/staff}}

{{api_endpoints:shop/site-events}}

{{api_endpoints:shop/pair}}

## База знань (лише для RAG-ПК)

Службові адреси, якими RAG-ПК забирає листи й документи та звітує про індексацію. Потрібен окремий ключ з правами
«База знань» ([База знань](guide:ai/knowledge-base)); іншим програмам вони не потрібні.

{{api_endpoints:knowledge}}

## Інші адреси

Адреси, яких немає в розділах вище (нові дії API з'являються тут самі, поки їх не описано окремо).

{{api_endpoints}}
