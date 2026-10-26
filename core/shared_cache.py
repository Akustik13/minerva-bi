"""Кеш, спільний для всіх процесів gunicorn (CACHES["shared"]); django.core.cache.cache — окремий у кожному процесі.

Для стану, який має бачити будь-який процес: завдання до RAG, кеш платних відповідей AI, лічильники спроб входу."""
from django.core.cache import caches
from django.utils.connection import ConnectionProxy

cache = ConnectionProxy(caches, "shared")
