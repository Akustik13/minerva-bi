# Minerva Business Intelligence System

> ERP/BI система для малого та середнього бізнесу.
> Управління продажами, складом, CRM, аналітика, DYMO-етикетки.

---

## Швидкий старт (Synology NAS / Linux)

### 1. Скопіювати файли на сервер

```bash
# Через SCP з Windows:
scp -r C:\tabele_mvp root@192.168.2.123:/volume1/docker/tabele_mvp

# Або через Synology File Station — завантажити папку проекту
```

### 2. Запустити

```bash
cd /volume1/docker/tabele_mvp
docker-compose up -d --build
```

Перший запуск (~30 сек):
- Чекає готовності PostgreSQL
- Виконує міграції БД
- Створює суперюзера `admin` / `Minerva`
- Запускає Django-сервер на порту `8000`

### 3. Відкрити у браузері

```
http://192.168.2.123:8000/admin/
```

Логін: `admin` · Пароль: `Minerva`

---

## Структура файлів

```
tabele_mvp/
├── docker-compose.yml    # Конфігурація Docker (DB + Web)
├── Dockerfile            # Образ Django-застосунку
├── requirements.txt      # Python-залежності
├── manage.py
├── tabele/               # Налаштування Django (settings.py, urls.py)
├── sales/                # Продажі та замовлення
├── crm/                  # Клієнти та RFM-аналіз
├── inventory/            # Склад та продукти
├── dashboard/            # Аналітика та графіки
├── bots/                 # DigiKey Bot, Nova Poshta API, AI
├── faq/                  # FAQ та ліцензія
├── labels_app/           # DYMO-етикетки
├── backup/               # Резервне копіювання БД
└── templates/            # HTML-шаблони
```

---

## Керування контейнерами

```bash
# Зупинити
docker-compose down

# Перезапустити (після зміни коду — не потребує rebuild)
docker-compose restart web

# Перебудувати образ (після зміни Dockerfile або requirements.txt)
docker-compose up -d --build

# Логи в реальному часі
docker-compose logs -f web

# Стан контейнерів
docker-compose ps
```

> **ВАЖЛИВО:** Ніколи не використовуйте `docker-compose down -v` — це видалить усі дані БД!

---

## Міграції

```bash
# Застосувати всі міграції
docker-compose exec web python manage.py migrate

# Створити нові міграції (після зміни моделей)
docker-compose exec web python manage.py makemigrations
docker-compose exec web python manage.py migrate
```

---

## Резервне копіювання

Резервні копії БД зберігаються в Docker volume `backup_data` (`/app/backups` в контейнері).

```bash
# Створити бекап вручну через exec:
docker-compose exec web python manage.py shell -c \
  "from backup.utils import create_backup; create_backup()"

# Переглянути наявні бекапи
docker-compose exec web ls -lh /app/backups/
```

---

## Відновлення з бекапу

```bash
# 1. Зупинити web
docker-compose stop web

# 2. Відновити БД з файлу backup.sql
docker-compose exec -T db psql -U tabele -d tabele < /path/to/backup.sql

# 3. Запустити web
docker-compose start web
```

---

## Зміна паролю адміна

Змінити в `docker-compose.yml` перед першим запуском:

```yaml
DJANGO_SUPERUSER_USERNAME: admin
DJANGO_SUPERUSER_EMAIL:    admin@minerva.local
DJANGO_SUPERUSER_PASSWORD: Minerva        # ← змінити!
```

Або через Django Admin: `/admin/auth/user/`

---

## Зміна Secret Key (для production)

В `docker-compose.yml`:

```yaml
DJANGO_SECRET_KEY: "замінити-на-довгий-випадковий-рядок"
```

Згенерувати:
```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```

---

## Доступ ззовні (Synology reverse proxy)

Synology DSM → Панель управління → Портал входу → Зворотний проксі:

| Джерело | Призначення |
|---------|-------------|
| `https://akustik.synology.me:81` | `localhost:8000` |

CSRF для зовнішнього домену вже налаштовано в `tabele/settings.py`:
```python
CSRF_TRUSTED_ORIGINS = ['https://akustik.synology.me', 'https://akustik.synology.me:81']
```

---

## Локальна розробка (Windows)

```powershell
# Активувати virtualenv
.\venv\Scripts\activate

# Запустити тільки БД в Docker, Django локально:
docker-compose up -d db
python manage.py runserver
```

---

## Порти

| Сервіс | Внутрішній | Зовнішній |
|--------|-----------|----------|
| Django | 8000 | 8000 |
| PostgreSQL | 5432 | **5433** (Synology DSM займає 5432) |

---

## Стек технологій

| Компонент | Технологія |
|-----------|-----------|
| Backend | Django 5.2, Python 3.11 |
| Database | PostgreSQL 16 |
| Frontend | Vanilla JS, Chart.js 4.4 |
| Контейнеризація | Docker + Docker Compose |
| Хостинг | Synology NAS DSM 7 |
