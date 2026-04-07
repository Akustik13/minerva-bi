from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ['*']
# ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
LABELS_DIR = BASE_DIR / 'media' / 'labels'


# DigiKey OAuth redirect URI (override для локального тестування)
# Приклад: DIGIKEY_OAUTH_REDIRECT_URI=http://localhost:8000/bots/digikey/oauth-callback/
DIGIKEY_OAUTH_REDIRECT_URI = os.getenv("DIGIKEY_OAUTH_REDIRECT_URI", "")

CSRF_TRUSTED_ORIGINS = [
    'https://akustik.synology.me',
    'https://akustik.synology.me:81',
    'http://192.168.2.123:8000',
]

# Synology reverse proxy — щоб Django знав що він за HTTPS проксі
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # ── Ядро системи ────────────────────────────────
    'core',             # 🔐 AuditLog + Ролі + Модулі

    # ── Ваші додатки (порядок = порядок у sidebar) ──
    'config',           # ⚙️ Конфігурація системи
    'crm',              # 👥 CRM
    'strategy',         # 🎯 Стратегії CRM
    'accounting',       # 💰 Бухгалтерія
    'sales',            # 🛒 Sales
    'shipping',         # 🚚 Доставка
    'inventory',        # 📦 Управління складом
    'dashboard',        # 📊 Dashboard
    'tasks',            # 📋 Задачі та нагадування
    'autoimport',       # 🔄 Авто-імпорт по розкладу
    'ai_assistant',     # 🏛️ Minerva AI
    'bots',             # 🤖 Боти та AI
    'faq',              # ❓ FAQ та підтримка
    'labels_app',       # 🏷️ Етикетки
    'backup',           # 💾 Резервне копіювання
    # ── REST API ──
    'rest_framework',
    'rest_framework.authtoken',
    'django_filters',
    'api',
    # ── Sites ──
    'django.contrib.sites',
]

SITE_ID = 1

_MIDDLEWARE_BASE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.AuditMiddleware",
    "core.middleware.ModuleAccessMiddleware",
    "config.middleware.OnboardingMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
# Whitenoise — тільки production (DEBUG=False), щоб не гальмувати локальний dev
if not DEBUG:
    _MIDDLEWARE_BASE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")
MIDDLEWARE = _MIDDLEWARE_BASE

ROOT_URLCONF = "tabele.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
            "core.context_processors.user_modules",
        ]},
    }
]

WSGI_APPLICATION = "tabele.wsgi.application"

if os.getenv("DB_HOST"):
    # Production (Docker / Synology) — PostgreSQL
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME", "tabele"),
            "USER": os.getenv("DB_USER", "tabele"),
            "PASSWORD": os.getenv("DB_PASSWORD", "tabele"),
            "HOST": os.getenv("DB_HOST", "localhost"),
            "PORT": os.getenv("DB_PORT", "5432"),
        }
    }
else:
    # Local dev без PostgreSQL — SQLite
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "uk"
TIME_ZONE = "Europe/Berlin"
USE_I18N = True
USE_TZ = True

STATIC_URL  = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
if not DEBUG:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
MEDIA_URL   = "/media/"
MEDIA_ROOT  = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTHENTICATION_BACKENDS = [
    "core.auth_backend.EmailOrUsernameBackend",
]

# ── Email ─────────────────────────────────────────────────────────────────────
# Console backend: лист з'явиться в docker logs, не на реальний email.
# Для реального SMTP — налаштуй NotificationSettings в адмін-панелі,
# або встанови EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
# та EMAIL_HOST / EMAIL_PORT / EMAIL_HOST_USER / EMAIL_HOST_PASSWORD через env.
EMAIL_BACKEND = 'config.email_backend.NotificationSettingsEmailBackend'
DEFAULT_FROM_EMAIL = 'Minerva BI <noreply@minerva-bi.local>'
PASSWORD_RESET_TIMEOUT = 259200  # 3 дні

# ── Локальний шлях для збереження документів замовлень ──────────────────────
# Файли зберігаються: LOCAL_DOCS_BASE_PATH / {source} / {DD.MM.YYYY} / {order_number} /
LOCAL_DOCS_BASE_PATH = os.getenv(
    "LOCAL_DOCS_BASE_PATH",
    r"C:\Users\prymv\Documents\projekt\Post und Zoll"
)

# ── Резервне копіювання ───────────────────────────────────────────────────────
BACKUP_DIR = os.getenv(
    "BACKUP_DIR",
    str(BASE_DIR / "backups")
)
# Ім'я Docker-контейнера з PostgreSQL (для `docker exec pg_dump`)
# Перевірити: docker ps --filter name=db
BACKUP_DB_CONTAINER = os.getenv("BACKUP_DB_CONTAINER", "tabele_mvp-db-1")
# Повний шлях до docker.exe (Django-процес може не бачити docker у PATH)
BACKUP_DOCKER_EXE = os.getenv(
    "BACKUP_DOCKER_EXE",
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
)

# ── Django REST Framework ─────────────────────────────────────────────────────
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "api.authentication.APIKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "api.permissions.HasAPIKeyScope",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": (
        [
            "rest_framework.renderers.JSONRenderer",
            "rest_framework.renderers.BrowsableAPIRenderer",
        ]
        if DEBUG
        else ["rest_framework.renderers.JSONRenderer"]
    ),
}
