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

from django.utils import timezone

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
    # видалити private/minerva-config.php — лише сайту з ключем магазину і коли чат без файлу не зупиниться
    out["remove_file"] = bool(bound and shop.assistant_remove_file and not can_remove_file(shop))
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


# ── Що фактично використовує кожен сайт (звіт сайту) і видалення файлу на сайтах ──────────────────────────────

def record_report(shop: Shop, params) -> None:
    """GET /shop/assistant/?site=…&file=…: сайт повідомляє, що в його private/minerva-config.php (без ключа)."""
    host = str(params.get("site") or "").strip().lower()[:120]
    if not host or not all(c.isalnum() or c in ".-:" for c in host):
        return
    report = {"at": timezone.now().isoformat(timespec="seconds"), "file": params.get("file") == "1",
              "file_enabled": params.get("file_enabled") == "1", "file_key": params.get("file_key") == "1",
              "file_url": str(params.get("file_url") or "")[:200]}
    sites = dict(shop.assistant_sites or {})
    sites[host] = report
    sites = dict(sorted(sites.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:10])
    update = {"assistant_sites": sites}
    # усі сайти повідомили, що файлу немає — видалення виконано
    if shop.assistant_remove_file and not any(r.get("file") for r in sites.values()):
        update["assistant_remove_file"] = False
    Shop.objects.filter(pk=shop.pk).update(**update)
    shop.assistant_sites = sites


def effective(shop: Shop, report: dict) -> dict:
    """Що сайт фактично використовує: {on, backend, endpoint, url_from, key_from}."""
    if shop.assistant_mode == Shop.ASSISTANT_SITE:
        return {"on": bool(report.get("file_enabled")), "backend": Shop.ASSISTANT_BRIDGE,
                "endpoint": f"{report['file_url']}/api" if report.get("file_url") else "", "url_from": "файл сайту",
                "key_from": "файл сайту" if report.get("file_key") else ""}
    if shop.assistant_backend == Shop.ASSISTANT_RAG_API:
        rag = rag_settings()
        return {"on": shop.assistant_mode == Shop.ASSISTANT_ON, "backend": Shop.ASSISTANT_RAG_API,
                "endpoint": f"{rag.base_url.rstrip('/')}/v1" if rag.base_url else "",
                "url_from": "налаштування помічника", "key_from": "налаштування помічника" if rag.api_key else ""}
    url = shop.assistant_url or report.get("file_url") or ""
    key_from = "Minerva" if shop.assistant_key else ("файл сайту" if report.get("file_key") else "")
    return {"on": shop.assistant_mode == Shop.ASSISTANT_ON, "backend": Shop.ASSISTANT_BRIDGE,
            "endpoint": f"{url}/api" if url else "", "url_from": "Minerva" if shop.assistant_url else "файл сайту",
            "key_from": key_from}


def can_remove_file(shop: Shop) -> str:
    """'' — файл на сайтах можна видалити, і чат без нього не зупиниться; інакше — чому ні."""
    if shop.assistant_mode == Shop.ASSISTANT_SITE:
        return "Спершу виберіть «Увімкнено» або «Вимкнено»: зараз сайт бере все саме з цього файлу."
    if shop.assistant_mode == Shop.ASSISTANT_ON and shop.assistant_backend == Shop.ASSISTANT_BRIDGE \
            and not (shop.assistant_url and shop.assistant_key):
        return "Спершу задайте тут адресу й ключ помічника — інакше без файлу чат на сайті зупиниться."
    return ""


# ── Перевірка RAG з Minerva (кнопка «🧪 Перевірити RAG») ───────────────────────────────────────────────────

def test_rag(shop: Shop, question: str = "", wait: int = 45) -> dict:
    """Minerva сама звертається до RAG, яким користуються сайти: доступність, ключ, за бажанням — тестове запитання."""
    import time

    checks = []

    def item(level, title, detail=""):
        checks.append({"level": level, "title": title, "detail": detail})

    question = (question or "").strip()[:500]
    rag_api = shop.assistant_mode != Shop.ASSISTANT_SITE and shop.assistant_backend == Shop.ASSISTANT_RAG_API
    name = "RAG API" if rag_api else "Веб-міст RAG"
    started = time.monotonic()
    try:
        (_test_rag_api if rag_api else _test_bridge)(shop, question, wait, item)
    except Exception as e:   # мережа, неочікувана відповідь — показати, не падати
        item("error", name, f"{type(e).__name__}: {str(e)[:200]}")
    errors = sum(c["level"] == "error" for c in checks)
    return {"summary": ("❌ " if errors else "✅ ") + name + (": є проблеми" if errors else ": працює"),
            "checks": checks, "seconds": round(time.monotonic() - started, 1)}


def _poll(get_job, wait, item):
    import time
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        data = get_job() or {}
        if data.get("status") == "completed":
            result = data.get("result") or {}
            item("ok", "Тестове запитання", f"Відповідь ({len(result.get('sources') or [])} джерел): "
                                            f"{str(result.get('answer') or '')[:400]}")
            return
        if data.get("status") == "failed":
            item("error", "Тестове запитання", f"RAG не зміг відповісти: {data.get('error') or 'failed'}")
            return
        time.sleep(2)
    item("warn", "Тестове запитання", f"Відповіді немає за {wait} с — RAG зайнятий або працює повільно.")


def _test_rag_api(shop, question, wait, item):
    from rag_assistant.client import RagClient, RagError
    rag = rag_settings()
    if not rag.configured:
        item("error", "RAG API", "У «⚙️ Налаштування помічника» (Minerva AI) не задано адресу або ключ.")
        return
    client = RagClient(rag, client_id=f"minerva-test:{shop.slug}")
    try:
        client.health()
        item("ok", "RAG API", f"{rag.base_url.rstrip('/')}/v1 відповідає, ключ прийнято.")
        if question:
            job = client.ask(question, [], language="de", latest=bool(rag.latest_only))
            _poll(lambda: client.job(job), wait, item)
    except RagError as e:
        item("error", "RAG API", f"{rag.base_url}: {e.message}")


def _test_bridge(shop, question, wait, item):
    import requests
    url = (shop.assistant_url or "").rstrip("/")
    if not url or not shop.assistant_key:
        item("warn", "Веб-міст RAG", "Адреса або ключ помічника задані лише у файлі сайту — Minerva їх не знає. "
                                     "Задайте їх тут або перевірте через «🔌 Перевірити зв'язок із сайтом».")
        return
    s = requests.Session()
    s.headers.update({"X-Minerva-Bridge": shop.assistant_key, "Accept": "application/json",
                      "User-Agent": "Minerva-RagTest/1.0"})
    r = s.get(url + "/api/session", timeout=(5, 15), allow_redirects=False)
    if r.status_code == 403:
        item("error", "Веб-міст RAG", f"{url}: ключ помічника не прийнято (HTTP 403).")
        return
    if r.status_code != 200:
        item("error", "Веб-міст RAG", f"{url}/api/session: HTTP {r.status_code}.")
        return
    csrf = (r.json() or {}).get("csrf", "")
    item("ok", "Веб-міст RAG", f"{url}/api відповідає, ключ прийнято.")
    if not question:
        return

    def post(path, body):
        return s.post(url + path, json=body, timeout=(5, 25), headers={"X-CSRF-Token": csrf}, allow_redirects=False)

    cid = post("/api/conversations", {}).json().get("id")
    r = post("/api/ask", {"conversation_id": cid, "question": question, "language": "de", "latest": True})
    if r.status_code != 202:
        item("error", "Тестове запитання", f"HTTP {r.status_code}: {r.text[:160]}")
        return
    job = r.json().get("job_id")
    _poll(lambda: s.get(f"{url}/api/jobs/{job}", timeout=(5, 20), allow_redirects=False).json(), wait, item)
