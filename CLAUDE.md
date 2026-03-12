# CLAUDE.md — Minerva Business Intelligence System

> Останнє оновлення: 2026-03-02

---

## 🎯 Проект

**Minerva** — ERP/BI система для малого бізнесу (e-commerce, дистрибуція).
Продажі + Склад + CRM (RFM) + Доставка + Аналітика + DYMO + Боти.

**Production:** Synology NAS `192.168.2.123`, Docker Compose, порт 81
**Local dev:** Windows, virtualenv, `http://localhost:8000`
**Автор:** Viacheslav Pryimak — `prym.via@gmail.com`

---

## 🛠 Стек

| Шар | Технологія |
|-----|-----------|
| Backend | Django 5.2, Python 3.10+ |
| Database | PostgreSQL 16 (DB: `tabele`) |
| Frontend | Vanilla JS, Chart.js 4.4, dark theme |
| DevOps | Docker + Docker Compose, Gunicorn |
| Libs | openpyxl, reportlab, python-barcode, Pillow |

---

## 📂 Структура додатків

```
tabele_mvp/
├── tabele/          # settings.py, urls.py, admin.py (sidebar order, site header)
├── crm/             # Customer, RFM-аналіз, signals ← → sales
├── sales/           # SalesOrder, SalesOrderLine, SalesSource, Excel import
├── shipping/        # Carrier, Shipment, Jumingo service stub
├── inventory/       # Product, Transaction, Supplier, PurchaseOrder, ReorderProxy
├── dashboard/       # Аналітика views + Chart.js, signals page, help, faq_index
├── bots/            # DigiKey Bot, Nova Poshta API, AI placeholder
├── faq/             # FAQ сторінка (managed=False, no DB table)
├── labels_app/      # DYMO 30252/30334, PDF генерація
├── backup/          # Резервне копіювання (placeholder + real backup logic)
├── templates/
│   └── admin/
│       └── base_site.html   # ← ГОЛОВНИЙ ФАЙЛ UI: sidebar, footer, motto, tooltip
└── media/orders/    # Документи: {source}/{order_number}/
```

**INSTALLED_APPS порядок:**
`crm` → `sales` → `shipping` → `inventory` → `dashboard` → `bots` → `faq` → `labels_app` → `backup`

**Sidebar порядок** керується в `tabele/admin.py` → `_get_app_list()` + `app_order` list.

---

## 🔑 Ключові моделі

### sales/
- `SalesOrder` — замовлення, поля: `source`, `status`, `shipping_deadline`, `shipped_at`, `affects_stock`, `order_date`
- `SalesOrderLine` — рядки: SKU, QTY, `total_price` (використовується в dashboard revenue)
- `SalesSource` — словник джерел (slug-based)

### crm/
- `Customer` — RFM scores (R/F/M), segment, status
- Дедуплікація: `external_key` = SHA256, метод `Customer.generate_key()`
- Auto-sync через **Django signals** при збереженні SalesOrder

### inventory/
- `Product` — SKU, reorder_point, ціни purchase/sale
- `InventoryTransaction` — Прихід/Витрата/Коригування
- `ReorderProxy` — managed=False, аналіз що замовити
- `Supplier`, `PurchaseOrder`, `PurchaseOrderLine`

### shipping/ (новий модуль, заготовка)
- `Carrier` — перевізник (Jumingo/DHL/UPS/FedEx), API credentials, дані відправника
- `Shipment` — відправлення прив'язане до SalesOrder
- `shipping/services/jumingo.py` — stub, реальний API не підключений
- Кнопка «🚚 Створити відправлення» є на change_form замовлення

### faq/ (placeholder, no DB)
- `models.py`: `managed = False`
- `admin.py`: override `get_urls()` → тільки `info_view`
- Сторінка: FAQ accordion, форма зворотнього зв'язку, нотатки (localStorage), issues tracker, **блок Послуги** (6 карток + mailto), ліцензія

---

## 🖥 UI / Sidebar архітектура

Весь sidebar будується JavaScript-ом у `templates/admin/base_site.html`:

- **GROUPS** — масив груп з `id`, `label`, `apps[]`, опційно `href` і `links[]`
- Групи: Аналітика (з links), CRM, Продажі, Доставка, Склад, AI та Боти, Система
- **Закріплений пункт** `❓ FAQ та підтримка` — `position: sticky; bottom: 0` — завжди видимий, лінк → `/admin/faq/faqplaceholder/`
- `_URL_GROUP` map → авто-розкриття активного розділу
- Collapse стан зберігається в `localStorage` (`mg-{id}`)
- Tooltip `.mtip` → `#mtip-float` floating div

**Sidebar групи і URL-маппінг:**
```
Аналітика  → /dashboard/, /dashboard/signals/
CRM        → /admin/crm/
Продажі    → /admin/sales/
Доставка   → /admin/shipping/
Склад      → /admin/inventory/
AI та Боти → /admin/bots/
Система    → /admin/backup/, /admin/auth/
```

---

## 📊 Dashboard

URL: `/dashboard/`
Фільтри: `date_from`, `date_to`, `source`, `category` + JS-пресети 30/60/90/365д
Base queryset: `SalesOrder.objects.filter(affects_stock=True)`
Revenue: `SalesOrderLine.objects.aggregate(Sum('total_price'))`

**Tile-сторінки:**
- `/dashboard/analytics/` — аналітика index
- `/dashboard/system/` — система index (кахлі: Backup, Auth, Довідник)
- `/dashboard/faq/` — faq index (кахлі: Довідник, FAQ)
- `/dashboard/help/` — Довідник (9 табів: Огляд, Дашборд, Продажі, CRM, Склад, Імпорт, Етикетки, Боти та API, Доставка, Швидкий старт)
- `/dashboard/signals/` — попередження системи

---

## ⚙️ Конфігурація (settings.py актуальний стан)

```python
TIME_ZONE = "Europe/Berlin"
LANGUAGE_CODE = "uk"
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = ['*']
CSRF_TRUSTED_ORIGINS = ['https://akustik.synology.me', 'https://akustik.synology.me:81', 'http://192.168.2.123:8000']
LOCAL_DOCS_BASE_PATH = r"C:\Users\prymv\Documents\projekt\Post und Zoll"
BACKUP_DB_CONTAINER = "tabele_mvp-db-1"
BACKUP_DOCKER_EXE = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
```

---

## 🚀 Команди

```powershell
# Локальна розробка (Windows)
.\venv\Scripts\activate
python manage.py runserver

# Міграції
python manage.py makemigrations <app>
python manage.py migrate
python manage.py check           # перевірка перед деплоєм

# Docker (production)
docker-compose up -d
docker-compose down              # ✅ зупинити
docker-compose down -v           # ❌ ЗАБОРОНЕНО — видаляє БД!
docker-compose exec web python manage.py migrate
```

---

## 📐 Архітектурні правила

1. **Django Admin як основний UI** — не писати окремий фронтенд
2. **Signals** для CRM ↔ Sales синхронізації — не дублювати логіку
3. **SHA256 external_key** — завжди при імпорті клієнтів
4. **affects_stock=True** — обов'язково для попадання в dashboard статистику
5. **Placeholder app pattern** (faq, ai, backup): `managed=False` + override `get_urls()`
6. **Sidebar групи** — змінювати тільки в `base_site.html` GROUPS масив
7. **Model order у sidebar** — `tabele/admin.py` → `model_order` dict у `_get_app_list()`
8. **Media:** `media/orders/{source}/{order_number}/` — структуру не змінювати
9. **RFM логіка** — тільки в `crm/utils.py`

---

## 🐛 Відомі проблеми та рішення

| Проблема | Рішення |
|---------|---------|
| CSRF помилка | Додати домен в `CSRF_TRUSTED_ORIGINS` у `settings.py` |
| БД порожня після restart | `docker-compose down` БЕЗ `-v` |
| Міграції конфліктують | `migrate <app> <номер> --fake` → `migrate` |
| Static files 404 | `python manage.py collectstatic` або `DJANGO_DEBUG=1` |
| `relation does not exist` | Placeholder app — override `get_urls()`, не викликати `changelist_view` |
| Edit tool "File has not been read" | Спочатку Read потрібного offset, потім Edit |
| `collectstatic` падає в Docker | Прибрати з startup command, використовувати `runserver` з `DEBUG=1` |

---

## 🗺 Roadmap (станом на 03.2026)

**Фаза 1 — Продукт (березень 2026):**
- [ ] Онбординг wizard (перший вхід)
- [ ] Demo-дані (заповнити тестовими замовленнями)
- [ ] Системні налаштування (назва компанії, валюта)
- [ ] Email/Telegram алерти (critical stock, дедлайни)

**Фаза 2 — Автоматизація (квітень–травень 2026):**
- [ ] Django REST Framework + API-ключі
- [ ] Jumingo реальна API інтеграція
- [ ] Webhook вхідні (Shopify/WooCommerce)
- [ ] Звіти PDF (рахунок-фактура, пакувальний лист)

**Фаза 3 — Продаж (червень 2026):**
- [ ] Мобільна адаптація Dashboard
- [ ] Права доступу по ролях
- [ ] Лендінг minerva-bi.com
- [ ] install.sh автодеплой скрипт

---

## 📌 Для Claude Code — стартові перевірки

- Читати `tabele/settings.py` перед зміною конфігурації
- Читати `templates/admin/base_site.html` перед зміною sidebar/footer/UI
- При змінах моделей → `makemigrations` + `python manage.py check`
- Зміни в `sales/` → перевіряти `crm/` (signals!)
- Новий app у sidebar → додати в GROUPS у `base_site.html` І в `app_order` у `tabele/admin.py`
- `docker-compose down -v` **ЗАБОРОНЕНО**
