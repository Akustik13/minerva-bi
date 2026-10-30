"""
Налаштування для автотестів:  python manage.py test api --settings=tabele.settings_test

Схема БД створюється напряму з моделей (без міграцій) в SQLite у пам'яті —
історія міграцій проєкту не накочується на чисту SQLite.
"""
from .settings import *  # noqa: F401,F403


class _DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MIGRATION_MODULES = _DisableMigrations()
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
WEBHOOKS_SYNC = True  # вебхуки в тестах відправляються без фонового потоку
WEBHOOK_RESOLVE_DNS = False  # адреси вебхуків у тестах — без DNS (api/webhooks.unsafe_target)
SHOP_MAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"  # листи кабінету покупців
# спільний кеш у тестах — той самий LocMem, що й default (однакова LOCATION): cache.clear() очищає обидва
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
          "shared": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
