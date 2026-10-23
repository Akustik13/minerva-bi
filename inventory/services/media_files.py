"""Фото й даташити товару: завантаження за посиланням і перевірка вмісту.

Тип визначається за вмістом (перші байти), а не за розширенням: посилання на кшталт
…/download.php?file=X.pdf віддають PDF, а сервер помилки — HTML-сторінку з кодом 200.
SVG не приймається (може містити скрипти, а сайт віддає файли зі свого домену).
"""
from __future__ import annotations

from urllib.parse import urlsplit

import requests

IMAGE, DATASHEET = "image", "datasheet"
MAX_BYTES = {IMAGE: 15 * 1024 * 1024, DATASHEET: 40 * 1024 * 1024}
USER_AGENT = "Mozilla/5.0 (compatible; Minerva-BI/1.0; +https://minerva-bi.com)"


class MediaError(Exception):
    """Зрозумілий текст для людини: чому файл не підходить."""


def sniff(data: bytes) -> tuple[str, str, str] | None:
    """(вид, розширення, content-type) за першими байтами або None."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return IMAGE, "jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return IMAGE, "png", "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return IMAGE, "gif", "image/gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return IMAGE, "webp", "image/webp"
    if b"%PDF-" in data[:1024]:
        return DATASHEET, "pdf", "application/pdf"
    return None


def _looks_like_html(data: bytes) -> bool:
    start = data[:512].lstrip().lower()
    return start.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml")) or b"<html" in start


def check(data: bytes, kind: str) -> tuple[str, str]:
    """Перевіряє вміст для виду kind → (розширення, content-type); інакше MediaError."""
    if not data:
        raise MediaError("порожній файл")
    if len(data) > MAX_BYTES[kind]:
        raise MediaError(f"файл завеликий (понад {MAX_BYTES[kind] // (1024 * 1024)} МБ)")
    found = sniff(data)
    if found and found[0] == kind:
        return found[1], found[2]
    want = "фото (JPG, PNG, WebP, GIF)" if kind == IMAGE else "PDF"
    if b"<svg" in data[:1024].lower():
        raise MediaError(f"SVG не підтримується — потрібно {want}")
    if _looks_like_html(data):
        raise MediaError(f"сервер повернув HTML-сторінку, а не {want}")
    if found:
        got = "PDF" if found[0] == DATASHEET else "фото"
        raise MediaError(f"це {got}, а потрібно {want}")
    raise MediaError(f"вміст не схожий на {want}")


def _download(url: str, limit: int, timeout, stop_at: int | None = None) -> bytes:
    """Вміст за посиланням (stop_at — лише початок файлу); MediaError з поясненням."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise MediaError("посилання має починатися з http:// або https://")
    try:
        with requests.get(url, stream=True, timeout=timeout, allow_redirects=True,
                          headers={"User-Agent": USER_AGENT, "Accept": "*/*"}) as resp:
            if resp.status_code != 200:
                raise MediaError(f"сервер відповів HTTP {resp.status_code}")
            chunks, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                chunks.append(chunk)
                if stop_at and size >= stop_at:
                    break
                if size > limit:
                    raise MediaError(f"файл завеликий (понад {limit // (1024 * 1024)} МБ)")
    except requests.exceptions.Timeout:
        raise MediaError("сервер не відповів вчасно") from None
    except requests.exceptions.SSLError:
        raise MediaError("помилка HTTPS-сертифіката") from None
    except requests.exceptions.ConnectionError:
        raise MediaError("немає з'єднання з сервером") from None
    except requests.exceptions.RequestException as e:
        raise MediaError(f"помилка запиту ({type(e).__name__})") from None
    return b"".join(chunks)


def fetch(url: str, kind: str, timeout: tuple[float, float] = (5, 30)) -> bytes:
    """Завантажує файл за посиланням і перевіряє вміст; MediaError з поясненням."""
    data = _download(url, MAX_BYTES[kind], timeout)
    check(data, kind)
    return data


def probe(url: str, kind: str, timeout: tuple[float, float] = (5, 15)) -> str:
    """Чи відповідає посилання і чи там справді фото / PDF — за початком файлу.
    '' — так; інакше текст помилки (як у MediaError)."""
    try:
        check(_download(url, MAX_BYTES[kind], timeout, stop_at=4096), kind)
    except MediaError as e:
        return str(e)
    return ""


def read_field(field, kind: str) -> bytes:
    """Вміст файлу з поля товару (FileField / ImageField) з перевіркою; MediaError, якщо його немає чи він не той."""
    if not field or not field.name:
        raise MediaError("файл не завантажено")
    try:
        field.open("rb")
        try:
            data = field.read(MAX_BYTES[kind] + 1)
        finally:
            field.close()
    except (FileNotFoundError, OSError):
        raise MediaError("файл відсутній на сервері Minerva") from None
    check(data, kind)
    return data
