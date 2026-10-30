"""Онлайн-курси валют — довідкові курси Європейського центрального банку (ЄЦБ), без ключа й реєстрації.

ЄЦБ публікує раз на робочий день (~16:00 CET) курси ~30 валют за 1 EUR:
https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml. Адреса фіксована (не від користувача).
`latest()` — {"date": "2026-10-30", "rates": {"EUR": "1", "USD": "1.0832", …}} з кешем; збій мережі — None
(і 10 хв без нових спроб), тож той, хто питає, бере свій останній збережений курс.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
SOURCE = "ЄЦБ"
CACHE_KEY = "fx-online:ecb"
CACHE_SECONDS = 3 * 3600
FAIL_KEY = "fx-online:ecb:failed"
FAIL_SECONDS = 600
TIMEOUT = 8

_TIME_RE = re.compile(r"""time=['"](\d{4}-\d{2}-\d{2})['"]""")
_RATE_RE = re.compile(r"""currency=['"]([A-Z]{3})['"]\s+rate=['"]([\d.]+)['"]""")


def _cache():
    from core.shared_cache import cache
    return cache


def _download() -> str:
    import requests
    r = requests.get(ECB_URL, timeout=TIMEOUT, allow_redirects=False)
    r.raise_for_status()
    return r.text


def parse(xml: str) -> dict | None:
    """Текст XML ЄЦБ → {"date", "rates"} або None, якщо це не курси."""
    rates = {}
    for cur, value in _RATE_RE.findall(xml or ""):
        try:
            v = Decimal(value)
        except InvalidOperation:
            continue
        if v > 0:
            rates[cur] = format(v.normalize(), "f")
    m = _TIME_RE.search(xml or "")
    if not rates or not m:
        return None
    rates["EUR"] = "1"
    return {"date": m.group(1), "rates": rates, "source": SOURCE}


def latest(force: bool = False) -> dict | None:
    """Свіжі курси ЄЦБ (кеш CACHE_SECONDS); None — не вдалося (мережа, сайт ЄЦБ, незнайомий формат)."""
    cache = _cache()
    if not force:
        hit = cache.get(CACHE_KEY)
        if hit:
            return hit
        if cache.get(FAIL_KEY):
            return None
    try:
        data = parse(_download())
    except Exception as exc:  # noqa: BLE001 — мережа / HTTP
        logger.warning("Курси ЄЦБ не завантажились: %s", exc)
        data = None
    if not data:
        cache.set(FAIL_KEY, 1, FAIL_SECONDS)
        return None
    cache.set(CACHE_KEY, data, CACHE_SECONDS)
    cache.delete(FAIL_KEY)
    return data


def per_eur(data: dict | None, currency: str) -> Decimal | None:
    """Скільки одиниць `currency` за 1 EUR у даних ЄЦБ."""
    try:
        v = Decimal(str((data or {}).get("rates", {}).get((currency or "").upper())))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v if v > 0 else None


def cross(data: dict | None, src: str, dst: str) -> Decimal | None:
    """Курс src → dst (1 src = N dst) через EUR, як рахує ЄЦБ; немає однієї з валют — None."""
    src, dst = (src or "").upper(), (dst or "").upper()
    if src == dst:
        return Decimal(1)
    a, b = per_eur(data, src), per_eur(data, dst)
    return None if a is None or b is None else b / a
