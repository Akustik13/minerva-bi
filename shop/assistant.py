"""Помічник Minerva на сайті магазину (чат RAG) — керування з картки магазину.

Сайт (api/minerva/index.php) раз на хвилину читає GET /api/v1/shop/assistant/ своїм ключем і:
- «Як налаштовано на сайті» (managed = false) — бере все з private/minerva-config.php, як раніше;
- «Увімкнено» — показує кнопку «Minerva»; адреса й ключ RAG — звідси, порожні — з файлу сайту;
- «Вимкнено» — кнопки на сайті немає.
Звідки відповіді (assistant_backend):
- rag_api — RAG API v1, як «Помічник по продукції» Minerva: адреса й ключ — з RagSettings (той самий публічний
  сервіс, напр. https://app.sevskiy.com), розмови зберігає сайт; працює з будь-якого хостингу;
- bridge — веб-міст RAG (X-Minerva-Bridge): адреса й ключ — з картки магазину або з файлу сайту.
Відповіді чату сервер сайту бере напряму в RAG; ключі віддаються лише ключу, прив'язаному до магазину
(ключі без прив'язки — RAG та інші системи — їх не отримують).
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from .models import Shop


def check_url(url: str) -> str:
    """Те саме правило, що й на сайті: https:// — будь-яка адреса, http:// — лише IP локальної мережі.
    '' — підходить, інакше текст помилки."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.username or parts.password or not parts.hostname or parts.query or parts.fragment:
        return "Потрібна адреса на кшталт https://rag.example.com або http://192.168.2.68:8790 (без логіна й параметрів)."
    if parts.scheme == "https":
        return ""
    if parts.scheme == "http":
        try:
            if ipaddress.ip_address(parts.hostname).is_private:
                return ""
        except ValueError:
            pass
        return "http:// дозволено лише до IP локальної мережі (напр. 192.168.…); інакше потрібен https://."
    return "Адреса має починатися з https:// (або http:// для IP локальної мережі)."


def key_is_bound(shop: Shop, key) -> bool:
    return key is not None and (key.shop_id == shop.pk or (bool(key.default_source) and key.default_source == shop.slug))


def rag_settings():
    from rag_assistant.models import RagSettings
    return RagSettings.get()


def public_config(shop: Shop, key=None) -> dict:
    """Відповідь GET /shop/assistant/ для сайту."""
    managed = shop.assistant_mode != Shop.ASSISTANT_SITE
    bound = managed and key_is_bound(shop, key)
    out = {
        "shop": shop.slug,
        "managed": managed,
        "enabled": shop.assistant_mode == Shop.ASSISTANT_ON,
        "backend": shop.assistant_backend if managed else Shop.ASSISTANT_BRIDGE,
        "upstream_url": shop.assistant_url if managed else "",
        "bridge_key": shop.assistant_key if bound else "",
    }
    if managed and shop.assistant_backend == Shop.ASSISTANT_RAG_API:
        rag = rag_settings()
        out.update(upstream_url=rag.base_url.rstrip("/"), bridge_key="", latest=bool(rag.latest_only),
                   api_key=rag.api_key if bound else "")
    return out


def describe(shop: Shop) -> str:
    """Короткий стан для картки магазину."""
    if shop.assistant_mode == Shop.ASSISTANT_SITE:
        return "керує сайт (private/minerva-config.php)"
    if shop.assistant_mode == Shop.ASSISTANT_OFF:
        return "вимкнено — кнопки на сайті немає"
    if shop.assistant_backend == Shop.ASSISTANT_RAG_API:
        rag = rag_settings()
        if not rag.configured:
            return "увімкнено, але в «⚙️ Налаштування помічника» не задано адресу або ключ RAG API"
        return f"увімкнено · RAG API помічника Minerva · {rag.base_url} · ключ з налаштувань помічника"
    url = shop.assistant_url or "адреса з файлу сайту"
    key = "ключ збережено" if shop.assistant_key else "ключ з файлу сайту"
    return f"увімкнено · {url} · {key}"
