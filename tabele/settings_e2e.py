"""
Ізольоване dev-середовище для наскрізного тесту Minerva ↔ приватний RAG (rag_assistant/e2e/README.md):
    python manage.py runserver 127.0.0.1:8010 --settings=tabele.settings_e2e

- окрема PostgreSQL minerva_e2e у тимчасовому контейнері (лише 127.0.0.1), схема з моделей без міграцій;
- окремі media і секрети в MINERVA_E2E_HOME (за замовчуванням ~/minerva-e2e), поза репозиторієм і спільною папкою;
- Django-пошта не надсилається; власні cookie, щоб не змішуватися з робочою сесією localhost.
Без E2E_DB_PASSWORD налаштування не завантажуються — випадково підключитися до робочої БД не можна.
"""
import os
from pathlib import Path

from .settings import *  # noqa: F401,F403


class _DisableMigrations:
    def __contains__(self, item):
        return True

    def __getitem__(self, item):
        return None


MINERVA_E2E = True
DEBUG = True
E2E_HOME = Path(os.getenv("MINERVA_E2E_HOME") or Path.home() / "minerva-e2e")
MIGRATION_MODULES = _DisableMigrations()
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.getenv("E2E_DB_NAME", "minerva_e2e"),
    "USER": "minerva_e2e",
    "PASSWORD": os.environ["E2E_DB_PASSWORD"],
    "HOST": "127.0.0.1",
    "PORT": os.getenv("E2E_DB_PORT", "55433"),
}}
MEDIA_ROOT = E2E_HOME / "media"
MINERVA_INSTANCE_ID = os.getenv("MINERVA_INSTANCE_ID") or "minerva-dev-e2e"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SESSION_COOKIE_NAME = "minerva_e2e_session"
CSRF_COOKIE_NAME = "minerva_e2e_csrftoken"
