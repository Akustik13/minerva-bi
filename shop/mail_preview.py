"""«👁 Приклад» листа в таблиці «Хто що кому шле» (картка магазину → «✉️ Пошта магазину»).

Від кого, кому, копія (CC), прихована копія (BCC), куди відповідають (Reply-To), тема, текст і вкладення — так, як
лист піде зараз, з тими значеннями таблиці, що на сторінці (ще не збереженими теж: apply_form). Дані покупця —
приклад. Листи Minerva — шаблони магазину (mail.compose / team_message), рахунок — invoice_service.sample_invoice;
листи сайту — тексти з самого сайту: підписаний запит на його вебхук (подія letters.preview — так само, як
«🔌 Перевірити зв'язок»), кеш CACHE_SECONDS. Нічого не надсилає й не зберігає.
"""
from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlsplit

from django.utils import timezone

from . import mail, mail_map
from .models import Shop

logger = logging.getLogger("shop")

BUYER = mail.SAMPLE["email"]   # приклад адреси покупця (Minerva); сайт підставляє свій приклад
LANGS = [("de", "Deutsch"), ("en", "English")]
SITE_LANGS = [("de", "Deutsch"), ("en", "English"), ("es", "Español"), ("fr", "Français"), ("it", "Italiano"),
              ("uk", "Українська"), ("ja", "日本語"), ("ko", "한국어"), ("zh", "中文"), ("tr", "Türkçe"), ("ar", "العربية")]
METHODS = [("prepayment", "Передоплата (Vorkasse)"), ("invoice", "Рахунок (Kauf auf Rechnung)"),
           ("cod", "Накладений платіж (Nachnahme)"), ("stripe", "Онлайн-оплата (Stripe)")]
SECRET_VARIANTS = [("verify", "Підтвердження email"), ("reset", "Новий пароль"), ("invite", "Запрошення від менеджера")]
CACHE_SECONDS = 120          # тексти сайту
FAIL_SECONDS = 30            # збій запиту до сайту — не повторювати одразу
TIMEOUT = 10
TEXT_MAX = 20000


class PreviewError(Exception):
    pass


def apply_form(shop: Shop, post) -> tuple[Shop, list[str]]:
    """(свіжий екземпляр магазину з незбереженими значеннями таблиці «Хто що кому шле», помилки в копіях) — у базу
    нічого не пише. post — уся форма картки (лише людині з правом змінювати магазин); без mail_letters_form — як
    збережено."""
    s = Shop.objects.select_related("mail_sender").get(pk=shop.pk)
    if not post or post.get("mail_letters_form") != "1":
        return s, []
    s.mail_letters, errors = mail.clean_copies(mail.copies_from_post(post))
    for field in ("mail_reply_to", "mail_from_name", "team_emails", "team_telegram"):
        if field in post:
            setattr(s, field, str(post.get(field) or "").strip())
    for flag in ("mail_welcome", "team_notify_registration", "team_notify_telegram", "site_order_confirmation"):
        setattr(s, flag, bool(post.get(flag)))   # прапорець форми: немає в POST — знято
    mail_map.save_routes(s, post)
    return s, errors


UNSAVED_FIELDS = ("mail_letters", "mail_reply_to", "mail_from_name", "team_emails", "team_telegram", "mail_welcome",
                  "team_notify_registration", "team_notify_telegram", "site_order_confirmation", "mail_sender_id",
                  "mail_routes", "site_mail_managed")


def unsaved(saved: Shop, now: Shop) -> bool:
    """Чи відрізняються значення таблиці на сторінці від збережених."""
    return any(getattr(saved, f) != getattr(now, f) for f in UNSAVED_FIELDS)


def _account_link(shop: Shop, lang: str, fragment: str = "") -> str:
    links = shop.account_links or {}
    base = links.get(lang) or next(iter(links.values()), "") or (shop.url or "https://shop.example").rstrip("/") + "/"
    return base + fragment


def _row(shop: Shop, key: str, st: dict) -> dict:
    for r in mail_map.customer_rows(shop, st) + mail_map.team_rows(shop, st):
        if r["key"] == key:
            return r
    raise PreviewError("Такого листа немає.")


def _active(shop: Shop, key: str, st: dict) -> tuple[bool, str]:
    """(чи надсилається зараз, чому ні)."""
    if key == "welcome" and not shop.mail_welcome:
        return False, "вимкнено галочкою «Надсилається»"
    if key == "team_registered" and not shop.team_notify_registration:
        return False, "вимкнено галочкою «Надсилається»"
    if key in ("order_customer", "quote_customer"):
        on = shop.site_order_confirmation if st["managed"] else st["confirm"]
        if not on:
            return False, "сайт не надсилає покупцю підтвердження"
    return True, ""


def _team_to(shop: Shop) -> list[str]:
    team = mail.addresses(shop.team_emails)
    return team or mail_map._notifications()["to"]


def _minerva(shop: Shop, key: str, lang: str, variant: str, row: dict) -> dict:
    """Лист Minerva: тема, текст, html, кому, копії."""
    lang = lang if lang in dict(LANGS) else "de"
    out = {"langs": LANGS, "lang": lang, "html": "", "attachments": [], "reply_to": "", "notes": []}
    vars_ = {k: v for k, v in mail.SAMPLE.items() if k != "name"}
    if key in mail.TEAM_LETTERS:   # команді — мова команди, без звертання
        out["lang"], out["langs"] = shop.team_lang if shop.team_lang in ("de", "en") else "de", []
        subject, text = mail.team_message(shop, key, **mail.SAMPLE)
        to = _team_to(shop)
        out.update(subject=subject, text=text, to=to)
        ns = mail_map._notifications()
        if shop.team_notify_telegram and ns["bot"] and (mail.team_chats(shop) or ns["chat"]):
            out["telegram"] = subject
        cc, bcc = mail.copies(shop, key, to)
    elif key == "invoice":
        try:
            from sales import invoice_service as inv_svc
        except ImportError as e:
            raise PreviewError("Рахунків у цій Minerva немає.") from e
        inv = inv_svc.sample_invoice(lang)
        subject, text = inv_svc.mail_text(inv)
        out.update(subject=subject, text=text, to=[inv.bill_to["email"]],
                   attachments=[f"{inv_svc.file_base(inv)}.pdf"], reply_to=shop.mail_reply_to)
        cc, bcc = mail.copies(shop, key, out["to"])
    else:
        kind = variant if key == "" else key
        if key == "" and kind not in dict(SECRET_VARIANTS):
            kind = "verify"
        link = {"verify": _account_link(shop, lang, "#verify=BEISPIEL"),
                "reset": _account_link(shop, lang, "#reset=BEISPIEL"),
                "invite": _account_link(shop, lang, "#reset=BEISPIEL"),
                "paid": _account_link(shop, lang, f"#order={mail.SAMPLE['number']}"),
                "deleted": ""}.get(kind, _account_link(shop, lang))
        subject, text, html = mail.compose(shop, kind, lang, name=mail.SAMPLE["name"], link=link, **vars_)
        out.update(subject=subject, text=text, html=html, to=[BUYER], reply_to=shop.mail_reply_to)
        if key == "":
            out.update(variants=SECRET_VARIANTS, variant=kind)
        cc, bcc = mail.copies(shop, kind, [BUYER])
    out.update(cc=cc, bcc=bcc)
    return out


# ── Тексти листів сайту (з самого сайту) ──────────────────────────────────────

def _site_hooks(shop: Shop):
    from api.models import Webhook
    from .connection import site_key
    hosts = {site_key(urlsplit(shop.url or "").hostname)} | {site_key(h) for h in (shop.site_mail_reports or {})}
    hosts.discard("")
    return [h for h in Webhook.objects.filter(is_active=True).order_by("pk")
            if site_key(urlsplit(h.url).hostname) in hosts]


def _clean_site(raw) -> dict:
    """Відповідь сайту → лише відомі листи й поля, обрізані рядки (дані — не інструкції, показуються текстом)."""
    out = {}
    for key in mail.SITE_LETTERS:
        x = raw.get(key) if isinstance(raw, dict) else None
        if not isinstance(x, dict):
            continue
        text = lambda k, n=500: str(x.get(k) or "")[:n]  # noqa: E731
        lst = lambda k: [str(a)[:200] for a in (x.get(k) or []) if isinstance(a, str)][:20]  # noqa: E731
        out[key] = {"subject": text("subject"), "body": text("body", TEXT_MAX), "from": text("from"),
                    "from_name": text("from_name"), "to": text("to"), "reply_to": text("reply_to"),
                    "to_customer": bool(x.get("to_customer")), "active": bool(x.get("active")),
                    "cc": lst("cc"), "bcc": lst("bcc"), "warnings": lst("warnings")}
    return out


def site_texts(shop: Shop, lang: str = "de", method: str = "prepayment") -> dict:
    """{"ok": True, "letters": {...}, "host"} або {"ok": False, "error": "…"} — листи сайту з тестовими даними.
    Запит: POST на вебхук сайту з підписом (X-Minerva-Signature, секрет вебхука), подія letters.preview."""
    import requests

    from api.webhooks import sign, unsafe_target
    from core.shared_cache import cache
    key = f"shop-mail-preview:{shop.pk}:{lang}:{method}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    hooks = _site_hooks(shop)
    if not hooks:
        return {"ok": False, "error": "На цей сайт немає вебхука — Minerva не може запитати тексти листів (REST API → "
                                      "Вебхуки або «🔗 Підключити сайт»)."}
    hook = hooks[0]
    problem = unsafe_target(hook.url, allow_private=not hook.api_key_id)
    if problem:
        return {"ok": False, "error": problem}
    delivery = str(uuid.uuid4())
    body = json.dumps({"id": delivery, "event": "letters.preview", "created_at": timezone.now().isoformat(),
                       "data": {"lang": lang, "method": method}}, ensure_ascii=False, separators=(",", ":"))
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "Minerva-Webhooks/1.0",
               "X-Minerva-Event": "letters.preview", "X-Minerva-Delivery": delivery,
               "X-Minerva-Signature": sign(hook.secret, body)}
    host = urlsplit(hook.url).hostname or ""
    try:
        r = requests.post(hook.url, data=body.encode("utf-8"), headers=headers, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        out = {"ok": False, "error": f"Сайт {host} не відповів: {type(e).__name__}"}
        cache.set(key, out, FAIL_SECONDS)
        return out
    if r.status_code == 401:
        out = {"ok": False, "error": f"Сайт {host} відхилив підпис (HTTP 401): секрет вебхука в Minerva не збігається з "
                                     "сайтом."}
    elif r.status_code != 200:
        out = {"ok": False, "error": f"Сайт {host} відповів HTTP {r.status_code}."}
    else:
        try:
            data = r.json()
        except ValueError:
            data = {}
        letters = _clean_site(data.get("letters")) if isinstance(data, dict) else {}
        out = ({"ok": True, "letters": letters, "host": host} if letters else
               {"ok": False, "error": f"Сайт {host} ще не вміє показувати тексти листів — потрібне оновлення файлів "
                                      "сайту (api/shop/webhook.php)."})
    cache.set(key, out, CACHE_SECONDS if out["ok"] else FAIL_SECONDS)
    return out


def _site(shop: Shop, key: str, lang: str, method: str, st: dict) -> dict:
    lang = lang if lang in dict(SITE_LANGS) else "de"
    method = method if method in dict(METHODS) else "prepayment"
    out = {"langs": SITE_LANGS, "lang": lang, "html": "", "attachments": [], "notes": []}
    if key == "order_customer":
        out.update(methods=METHODS, method=method)
    got = site_texts(shop, lang, method)
    site = (got.get("letters") or {}).get(key) or {}
    if got["ok"]:
        out.update(subject=site.get("subject", ""), text=site.get("body", ""))
        out["notes"] += site.get("warnings") or []
    else:
        out.update(subject="", text="")
        out["notes"].append(got["error"] + " Текст листа — на сайті: /admin/ → «📨 Листи».")
    customer = key in ("order_customer", "quote_customer")
    sample = "einkauf@musterfirma.example"   # приклад покупця на сайті (api/admin/index.php admin_letters)
    if st["managed"]:   # усе задає Minerva — з незбереженими значеннями таблиці
        if customer:
            to = [sample]
            out["reply_to"] = shop.mail_reply_to or (site_mail_from(shop) or "")
        else:
            to = (mail.addresses(shop.team_emails) or mail.addresses((st["report"] or {}).get("notify"))
                  if key != "contact_team" else mail.addresses(st["recipient"].replace("—", "")))
            out["reply_to"] = sample
        cc, bcc = mail.copies(shop, key, to)
        out.update(to=to, cc=cc, bcc=bcc)
    else:               # налаштування сайту — як каже сам сайт
        to = [sample] if customer else mail.addresses(site.get("to") or (st["notify"] if key != "contact_team"
                                                                            else st["recipient"]).replace("—", ""))
        out.update(to=to, cc=site.get("cc") or [], bcc=site.get("bcc") or [], reply_to=site.get("reply_to") or "")
        out["notes"].append("Листи сайту зараз ідуть з налаштувань сайту — копії з цієї таблиці не діють (① «🌐 Листи "
                            "сайту»).")
    if st["managed"] and not st["letters_ok"] and (out["cc"] or out["bcc"]):
        out["notes"].append("Сайт зі старими файлами: копії кожного листа ще не діють (лише прихована копія "
                            "підтвердження замовлення).")
    return out


def site_mail_from(shop: Shop) -> str:
    from . import site_mail
    acc = site_mail.sender_account(shop)
    return acc.email_address if acc is not None else ""


def preview(shop: Shop, key: str, lang: str = "de", method: str = "prepayment", variant: str = "") -> dict:
    """Приклад листа: {key, title, when, who, from, to, cc, bcc, reply_to, subject, text, html, attachments, telegram,
    active, inactive_reason, notes, langs, lang, methods, method, variants, variant}."""
    st = mail_map.site_state(shop)
    row = _row(shop, key, st)
    if row.get("site"):
        body = _site(shop, key, lang, method, st)
    else:
        body = _minerva(shop, key, lang, variant, row)
    active, why = _active(shop, key, st)
    if not body.get("to"):
        active, why = False, why or "нікому — немає адрес"
    out = {"key": key, "title": row["title"], "when": row["when"], "who": row["who"], "from": row["sender"],
           "active": active, "inactive_reason": why, "notes": [], "telegram": "", "methods": [], "method": "",
           "variants": [], "variant": ""}
    out.update(body)
    out["to_example"] = key not in mail.TEAM_LETTERS
    out["bcc_allowed"] = key == "" or key in mail.CUSTOMER_LETTERS
    if key == "":
        out["notes"].append("Без копій: у листі одноразове посилання — його бачить лише покупець.")
    if key == "invoice" and not mail_map._invoice_auto():
        out["notes"].append("Сам рахунок покупцю не йде — лише кнопкою в 🧾 Рахунках (галочка «🧾 Надсилати рахунок "
                            "покупцю автоматично»).")
    return out
