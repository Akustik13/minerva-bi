"""Помічник Minerva на сайті магазину (чат RAG) — керування з картки магазину.

Сайт (api/minerva/index.php) раз на хвилину читає GET /api/v1/shop/assistant/ своїм ключем і:
- «Як налаштовано на сайті» (managed = false) — бере все з private/minerva-config.php, як раніше;
- «Увімкнено» — показує кнопку «Minerva»; адреса й ключ RAG — звідси, порожні — з файлу сайту;
- «Вимкнено» — кнопки на сайті немає.
Відповіді чату сервер сайту бере напряму в RAG; ключ помічника віддається лише ключу, прив'язаному до магазину
(ключі без прив'язки — RAG та інші системи — його не отримують).
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


def public_config(shop: Shop, key=None) -> dict:
    """Відповідь GET /shop/assistant/ для сайту."""
    managed = shop.assistant_mode != Shop.ASSISTANT_SITE
    return {
        "shop": shop.slug,
        "managed": managed,
        "enabled": shop.assistant_mode == Shop.ASSISTANT_ON,
        "upstream_url": shop.assistant_url if managed else "",
        "bridge_key": shop.assistant_key if managed and key_is_bound(shop, key) else "",
    }


def describe(shop: Shop) -> str:
    """Короткий стан для картки магазину."""
    if shop.assistant_mode == Shop.ASSISTANT_SITE:
        return "керує сайт (private/minerva-config.php)"
    if shop.assistant_mode == Shop.ASSISTANT_OFF:
        return "вимкнено — кнопки на сайті немає"
    url = shop.assistant_url or "адреса з файлу сайту"
    key = "ключ збережено" if shop.assistant_key else "ключ з файлу сайту"
    return f"увімкнено · {url} · {key}"
