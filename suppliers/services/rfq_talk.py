"""💬 Швидке листування з постачальником запиту (RFQ) і ↪ пересилання вибраних пропозицій колезі.

Сторінка запиту (suppliers/admin.py work_view): «💬» у рядку — вікно лише з листуванням цього постачальника
(усі гілки рядка, rfq_replies.mail), праворуч — «🧭 Коротко» (AI) і коротка історія; відповідь іде в ту саму гілку.
Галочки рядків → «↪ Переслати колезі» — усе вибране одним листом.

  letters(r, user)          — листи рядка за датою; текст і тему бачить лише власник скриньки (rfq_replies.can_read),
                              чужі — лише «лист у скриньці колеги»;
  target(req, r, letters)   — кому відповідати: автор останнього листа постачальника (frank@… замість charles@…),
                              копія — адреса запиту, якщо вона інша; тема «Re: …»; на який лист (In-Reply-To);
  history(req, r, timing)   — коротка історія: надіслали, відповів за N, нагадали, розібрано AI, вибрано, події;
  brief_cached / brief      — «🧭 Коротко»: про що питають, що обіцяли вони / ми, на що треба відповісти, чернетка —
                              AI (ai.chat_brief), лише з власних листів людини; кеш core.shared_cache на людину й
                              рядок, діє, поки листування не змінилось (fingerprint);
  reply(...)                — лист у гілку (In-Reply-To), підпис скриньки, копія в Minerva (rfq.store_sent), подія;
  forward_plan / forward    — вибрані рядки: таблиця пропозицій, листування (лише ваші скриньки), файли
                              постачальників до rfq.ATTACH_MAX_MB (решта — назвами) → один лист колезі;
  originals_plan / forward_originals — «📨 Листи як є»: кожен лист вибраних (лише ваші скриньки, і наші, і їхні)
                              вкладенням .eml, як він на сервері пошти (email_assistant/originals.py: текст, картинки,
                              файли), файли постачальників без такого листа — звичайними вкладеннями; понад
                              rfq.ATTACH_MAX_MB разом — кількома листами «(1/2)» (_pack), у фоні (jobs);
  compose_each / send_each  — ✉️ «Написати кожному»: вибраним постачальникам — кожному свій лист відповіддю в його
                              гілці (target: автор останнього листа, копія — лише адреса цієї ж фірми), з вашої
                              скриньки; текст — AI за побажанням людини (ai.each_letter, мовою листування, на ім'я)
                              або один шаблон ({greeting} {name} {company} {code} — fill); людина бачить і правит
                              кожен лист до надсилання, інші постачальники його не бачать;
  save_letters / update_letters / saved_letters — 📝 складені листи зберігаються (SupplierRequestLetter, своє в
                              кожної людини): правки — автозбереженням, надіслане позначається (reply), невідправлене
                              відкривається знову без нового складання AI («📝 Збережені листи» над таблицею, 💬).
"""
import hashlib
import logging
import re
import time

from django.utils import timezone
from django.utils.html import escape

logger = logging.getLogger(__name__)

BRIEF_TTL = 30 * 86400
BRIEF_MAILS = 12             # найновіших листів у «🧭 Коротко»
LETTER_CHARS = 6000          # тексту одного листа у вікні / пересиланні
FORWARD_MAILS = 30           # листів одного постачальника в пересиланні
FORWARD_MAX_ROWS = 30
ORIGINALS_MAX = 60           # листів одного постачальника «📨 Листи як є»
ORIGINALS_PARTS_MAX = 8      # листів колезі за одне пересилання (кожен до rfq.ATTACH_MAX_MB)
EACH_MAX = 30                # листів «✉️ Написати кожному» за раз
EACH_PAUSE = 2               # с між листами: кожен лист — окремий вхід на SMTP, 13 підряд сервер може відкинути
EACH_RETRY = (20, 60)        # тимчасова відмова сервера пошти (4xx, «too many», «try again») — повтор через N с
_TRANSIENT_RE = re.compile(r"\b4\d\d\b|too many|rate.?limit|limit exceeded|try again|temporar|throttl|"
                           r"timed? ?out|connection (?:reset|closed|refused)|unexpectedly closed|"
                           r"Не вдалось підключитись", re.I)


class TalkError(Exception):
    pass


# ── листування рядка ──

def letters(r, user) -> list:
    from django.urls import reverse
    from suppliers.services import rfq_replies
    own = rfq_replies._own()
    # 📎 файли постачальника, уже збережені в запиті, — їх бачать усі, хто бачить запит; решта — лише зі своїх листів
    saved = {(f.message_id, f.index): reverse("admin:suppliers_supplierrequest_file", args=[r.request_id, "reply", f.pk])
             for f in r.reply_files.all() if f.file}
    bank_ids = {(f.message_id, f.index): f"reply:{f.pk}" for f in r.reply_files.all() if f.file}   # 📂 вікно файлів
    out = []
    for m in rfq_replies.mail(r):
        readable = rfq_replies.can_read(m, user)
        ours = not rfq_replies.theirs(m, own)
        out.append({
            "m": m, "pk": m.pk, "at": m.sent_at, "ours": ours, "readable": readable,
            "first": m.pk == r.sent_message_id,
            "who": (m.from_name or m.from_email) if readable else ("ми" if ours else r.supplier.name),
            "email": m.from_email if readable else "",
            "to": ", ".join(m.to_emails or []) if readable and isinstance(m.to_emails, list) else "",
            "subject": (m.subject or "") if readable else "",
            "text": rfq_replies.snippet(m, LETTER_CHARS) if readable else "",
            "files": [{"name": a.get("name"), "icon": rfq_replies.icon(a.get("name")),
                       "size": rfq_replies.size_text(a.get("size")),
                       "bank_id": bank_ids.get((m.pk, i), ""),
                       "url": saved.get((m.pk, i)) or (reverse("admin:suppliers_supplierrequest_chat_file",
                                                               args=[r.request_id, r.pk, m.pk, i]) if readable else "")}
                      for i, a in enumerate(m.attachments or []) if rfq_replies.wanted(a)],
        })
    return out


def _re(subject) -> str:
    s = " ".join(str(subject or "").split())          # тема з чужої пошти буває з переносом рядка
    return s if s.lower().startswith(("re:", "aw:", "antw:")) else f"Re: {s}"


def target(req, r, items, user=None) -> dict:
    """Кому відповідати й на який лист. Немає жодного листа, який людина бачить, — на адресу запиту, нова гілка."""
    from suppliers.services import rfq
    last_in = next((x for x in reversed(items) if not x["ours"] and x["readable"]), None)
    base = last_in or next((x for x in reversed(items) if x["readable"]), None)
    to = (last_in["m"].from_email if last_in else r.email) or r.email
    cc = r.email if r.email and r.email.lower() != (to or "").lower() else ""
    subject = _re(base["m"].subject if base and base["m"].subject else rfq.render(req, r)[0])
    acc = base["m"].account if base and base["m"].account.is_active else (
        rfq.account_for(user, req) if user is not None else None)
    return {"to": to, "cc": cc, "subject": subject, "reply_pk": base["pk"] if base else None,
            "from": getattr(acc, "email_address", "")}


def history(req, r, tm) -> list:
    """Коротка історія рядка — найновіше вгорі: ключові дати + події запиту про цього постачальника."""
    from suppliers.models import SupplierEvent
    out = []
    if r.sent_at:
        out.append({"at": r.sent_at, "text": "📤 Надіслали запит"})
    if r.replied_at:
        out.append({"at": r.replied_at, "text": "📥 Перша відповідь" + (f" — за {tm['after_text']}"
                                                                       if tm.get("after_text") else "")})
    if tm.get("newer") and tm.get("last_in"):
        out.append({"at": tm["last_in"], "text": "📥 Останній лист постачальника"})
    if r.reminded_at:
        out.append({"at": r.reminded_at, "text": "⏰ Нагадали"})
    ai_at = (r.ai or {}).get("at")
    if ai_at:
        try:
            import datetime
            out.append({"at": datetime.datetime.fromisoformat(ai_at), "text": "🧠 Пропозицію розібрано AI"})
        except ValueError:
            pass
    for e in SupplierEvent.objects.filter(request=req, supplier=r.supplier).select_related("user")[:20]:
        who = (e.user.get_full_name() or e.user.username) if e.user_id else "Minerva"
        out.append({"at": e.created_at, "text": f"{e.text} — {who}"})
    out = [x for x in out if x["at"]]
    out.sort(key=lambda x: x["at"], reverse=True)
    seen, uniq = set(), []
    for x in out:                      # «📤 Надіслали» і подія про те саме — один раз за хвилину
        key = (x["at"].replace(second=0, microsecond=0), x["text"][:2])
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq[:25]


# ── 🧭 Коротко (AI) ──

def fingerprint(items) -> str:
    return hashlib.sha1(",".join(str(x["pk"]) for x in items if x["readable"]).encode()).hexdigest()[:16]


def _key(user, r) -> str:
    return f"rfq-brief:{user.pk}:{r.pk}"


def brief_cached(user, r, items):
    """Збережене «🧭 Коротко» людини для рядка → {data, at, fresh}; None — ще не складали."""
    from core.shared_cache import cache
    got = cache.get(_key(user, r))
    if not isinstance(got, dict) or not got.get("data"):
        return None
    from suppliers.services.rfq_usage import label
    return {**got, "fresh": got.get("fp") == fingerprint(items), "at_dt": _when(got.get("at")),
            "label": label(got.get("model")), "usd": f"${float((got.get('data') or {}).get('cost_usd') or 0):.3f}"}


def _when(iso):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(iso or ""))
    except ValueError:
        return None


def brief(req, r, user, model=None) -> dict:
    from core.shared_cache import cache
    from suppliers.services import ai, rfq_compare
    items = letters(r, user)
    mine = [x for x in items if x["readable"]]
    if not mine:
        raise TalkError("Листування з цим постачальником у ваших скриньках немає.")
    row = next((x for x in rfq_compare.table(req)["rows"] if x["recipient"].pk == r.pk), None)
    data = ai.chat_brief(req, r, mine[-BRIEF_MAILS:], _offer(row, r), user=user, model=model)
    from suppliers.services import rfq_usage
    rfq_usage.add(req, "brief", ai.model_of(model), data.get("cost_usd"))
    value = {"data": data, "fp": fingerprint(items), "at": timezone.now().isoformat(), "model": ai.model_of(model)}
    cache.set(_key(user, r), value, BRIEF_TTL)
    return {**value, "fresh": True, "at_dt": _when(value["at"]), "label": rfq_usage.label(value["model"]),
            "usd": f"${float(data.get('cost_usd') or 0):.3f}"}


def _offer(row, r) -> str:
    """Ціни постачальника з таблиці одним рядком (для AI) — «5 шт: 1,92 USD/шт; 2500 шт: …»."""
    if not row or not row["priced"]:
        return ""
    return "; ".join(f"{c['qty']} шт: {c['unit_text']} {r.currency}/шт" for c in row["cells"] if not c.get("missing"))


# ── 💬 AI у полі відповіді ──

IMPROVE_WISH = ("Виправ і покращ мій чорновий текст листа нижче: грамотно, ввічливо, мовою листування з цим "
                "постачальником; зміст, цифри й прохання — як у мене, нічого не додавай і не прибирай.")


def ai_reply(req, r, user, wish="", text="", mode="write", model=None) -> dict:
    """«✍️ Написати» (лист за побажанням людини) або «✨ Покращити мій текст» — з листування цього постачальника
    (лише власні скриньки), мовою листування, на ім'я → {body, usd, model}. Нічого не надсилає."""
    from suppliers.services import ai, rfq_compare, rfq_usage
    items = letters(r, user)
    mine = [x for x in items if x["readable"]]
    if mode == "improve":
        if not (text or "").strip():
            raise TalkError("Спершу напишіть текст — AI його виправить і покращить.")
        wish = IMPROVE_WISH + "\nМІЙ ТЕКСТ:\n" + text.strip()[:4000]
    elif not (wish or "").strip():
        raise TalkError("Напишіть, що відповісти, — напр. «подякуй і запитай ціну з позолотою контактів».")
    row = next((x for x in rfq_compare.table(req)["rows"] if x["recipient"].pk == r.pk), None)
    got = ai.each_letter(req, r, mine[-BRIEF_MAILS:], _offer(row, r), wish.strip(), _person(r, items),
                         user=user, model=model)
    rfq_usage.add(req, "reply", ai.model_of(model), got.get("cost_usd"))
    if not got.get("body"):
        raise TalkError("AI не склав лист — спробуйте ще раз або напишіть самі.")
    return {"body": got["body"], "usd": float(got.get("cost_usd") or 0), "model": rfq_usage.label(ai.model_of(model))}


# ── ✉️ відповідь у гілку ──

def reply(req, r, user, to, cc, subject, body, reply_pk=None) -> dict:
    from email_assistant.models import EmailMessage
    from email_assistant.smtp_client import SMTPClient
    from suppliers.models import SupplierEvent
    from suppliers.services import rfq, rfq_replies
    to_list, cc_list = rfq.addresses(to), rfq.addresses(cc)
    if not to_list:
        raise TalkError("Вкажіть, кому — адресу постачальника.")
    body = (body or "").strip()
    if not body:
        raise TalkError("Порожній лист.")
    prev = None
    if reply_pk:
        allowed = {m.pk for m in rfq_replies.mail(r)}
        prev = EmailMessage.objects.select_related("account", "thread").filter(
            pk=int(reply_pk), account__user=user).first() if int(reply_pk) in allowed else None
    account = prev.account if prev is not None and prev.account.is_active else rfq.account_for(user, req)
    if account is None:
        raise TalkError("У вас немає активної поштової скриньки в Minerva.")
    quote = ""
    if prev is not None:
        from email_assistant.quotes import new_text
        head = f"{prev.sent_at:%d.%m.%Y %H:%M}, {prev.from_name or prev.from_email}:" if prev.sent_at else ""
        quote = head + "\n" + "\n".join("> " + ln for ln in new_text(prev.body_text or "").splitlines()[:60])
    text, html = rfq.letter(account, body, quote)
    subject = " ".join(str(subject or "").split()) or _re(prev.subject if prev else rfq.render(req, r)[0])
    try:
        result = SMTPClient(account).send(to_emails=to_list, cc_emails=cc_list, subject=subject, body_text=text,
                                          body_html=html, reply_to_message=prev, signature=False)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e)}
    if not result.get("ok"):
        raise TalkError("Не надіслано: " + str(result.get("error") or "помилка SMTP")[:400])
    msg = None
    try:
        from django.db import transaction
        with transaction.atomic():          # копія не збереглась — лист уже пішов, запит не ламаємо
            msg = rfq.store_sent(account, result, to_list, cc_list, subject, text, [], reply_to=prev, body_html=html)
    except Exception:  # noqa: BLE001
        logger.warning("rfq talk: store reply failed", exc_info=True)
    if msg is not None and msg.thread_id:
        from suppliers.services.rfq_replies import threads
        if not r.thread_id:
            type(r).objects.filter(pk=r.pk).update(thread=msg.thread)
        elif msg.thread_id not in threads(r):
            r.extra_threads.add(msg.thread)
    SupplierEvent.objects.create(supplier=r.supplier, request=req, kind=SupplierEvent.MAIL, user=user, message=msg,
                                 text=f"✉️ Відповіли постачальнику → {', '.join(to_list)}")
    from suppliers.models import SupplierRequestLetter
    SupplierRequestLetter.objects.filter(recipient=r, user=user, sent_at=None).update(sent_at=timezone.now(), error="")
    return {"ok": True, "to": to_list, "cc": cc_list, "subject": subject}


# ── ✉️ написати кожному ──

def _person(r, items) -> str:
    """До кого звертаємось: автор останнього листа постачальника (ім'я без посади), інакше контакт запиту."""
    from suppliers.services import rfq
    last_in = next((x for x in reversed(items) if not x["ours"] and x["readable"]), None)
    if last_in is not None:
        n = (last_in["m"].from_name or "").strip()
        if n and "@" not in n:
            p = rfq.person_name(n)
            if p:
                return p.title() if p.islower() else p        # «frank» → «Frank»
    return rfq.contact_name(r)


def fill(req, r, items, text) -> str:
    """Шаблон одного тексту для всіх: {greeting} — привітання на ім'я мовою запиту, {name}, {company}, {code}."""
    from suppliers.services import rfq
    t = rfq.TEXTS.get(req.language, rfq.TEXTS["en"])
    name = _person(r, items)
    values = {"greeting": t["hello"].format(name=name) if name else t["hello_all"], "name": name or r.supplier.name,
              "company": r.supplier.name, "code": req.code or ""}
    return re.sub(r"\{(greeting|name|company|code)\}", lambda m: values[m.group(1)], text or "")


def compose_each(req, rids, user, wish, mode="ai", model=None, report=None) -> list:
    """Лист кожному вибраному (ще нічого не надсилає) → [{rid, name, to, cc, subject, reply, from, body, note,
    error}]. Невідправлені рядки (запит ще не пішов) пропускаються."""
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services import ai
    wish = (wish or "").strip()
    if not wish:
        raise TalkError("Напишіть, що сказати постачальникам.")
    _t, rows = _rows(req, rids)
    rows = [row for row in rows if row["recipient"].state not in (R.PLANNED, R.FAILED)][:EACH_MAX]
    if not rows:
        raise TalkError("Вибраним ще не надсилали запит — спершу «📤 Надіслати».")
    out = []
    for i, row in enumerate(rows, 1):
        r = row["recipient"]
        if report:
            report(f"✍️ {i} / {len(rows)} · {r.supplier.name}")
        items = letters(r, user)
        tg = target(req, r, items, user)
        mine = [x for x in items if x["readable"]]
        item = {"rid": r.pk, "name": r.supplier.name, "to": tg["to"], "cc": tg["cc"], "subject": tg["subject"],
                "reply": tg["reply_pk"] or "", "from": tg["from"], "body": "", "note": "", "error": ""}
        if not items:
            item["note"] = "Листів з ним у Minerva немає — лист піде новою гілкою, без цитати."
        elif not mine:
            item["note"] = "🔒 Листування — у скриньці колеги: лист піде новою гілкою, без цитати."
        if mode == "template":
            item["body"] = fill(req, r, items, wish)
        else:
            try:
                got = ai.each_letter(req, r, mine[-BRIEF_MAILS:], _offer(row, r), wish, _person(r, items),
                                     user=user, model=model)
                item["body"] = got["body"]
                from suppliers.services import rfq_usage
                rfq_usage.add(req, "letters", ai.model_of(model), got.get("cost_usd"))
                item["usd"] = float(got.get("cost_usd") or 0)
            except ai.AIError as e:
                item["error"] = str(e)[:300]
            if not item["body"]:
                item["error"] = item["error"] or "AI не склав лист — напишіть самі."
        out.append(item)
    save_letters(req, user, out, wish=wish, mode=mode)     # 📝 складене не губиться — дослати можна пізніше
    return out


def _letter_item(x) -> dict:
    return {"rid": x.recipient_id, "name": x.recipient.supplier.name, "to": x.to, "cc": x.cc, "subject": x.subject,
            "body": x.body, "reply": x.reply_message_id or "", "from": "", "note": x.note, "error": x.error,
            "on": x.send, "sent": bool(x.sent_at)}


def save_letters(req, user, items, wish="", mode=""):
    """Складені листи → SupplierRequestLetter (по одному на рядок і людину; новий склад замінює старий)."""
    from email_assistant.models import EmailMessage
    from suppliers.models import SupplierRequestLetter
    rows = {r.pk: r for r in req.recipients.all()}
    for it in items:
        r = rows.get(int(it["rid"]) if str(it.get("rid") or "").isdigit() else 0)
        if r is None:
            continue
        rp = int(it["reply"]) if str(it.get("reply") or "").isdigit() else None
        reply = EmailMessage.objects.filter(pk=rp, account__user=user).first() if rp else None   # лише свої листи
        SupplierRequestLetter.objects.update_or_create(recipient=r, user=user, defaults={
            "request": req, "to": str(it.get("to") or "")[:500], "cc": str(it.get("cc") or "")[:500],
            "subject": " ".join(str(it.get("subject") or "").split())[:500], "body": str(it.get("body") or ""),
            "reply_message": reply, "wish": wish[:4000], "mode": mode[:10], "send": not it.get("error") or
            bool(it.get("body")), "note": str(it.get("note") or "")[:300], "error": str(it.get("error") or ""),
            "sent_at": None})


def update_letters(req, user, items) -> int:
    """✏️ Правки у вікні (автозбереження): Кому, Копія, Тема, Текст, «Надіслати цьому» — лише ще не надіслані."""
    from suppliers.models import SupplierRequestLetter
    n = 0
    for it in items:
        if not isinstance(it, dict) or not str(it.get("rid") or "").isdigit():
            continue
        n += SupplierRequestLetter.objects.filter(request=req, user=user, recipient_id=int(it["rid"]),
                                                  sent_at=None).update(
            to=str(it.get("to") or "")[:500], cc=str(it.get("cc") or "")[:500],
            subject=" ".join(str(it.get("subject") or "").split())[:500], body=str(it.get("body") or ""),
            send=bool(it.get("on", True)))
    return n


def saved_letters(req, user) -> dict:
    """📝 Невідправлені збережені листи людини в цьому запиті → {letters, at, wish}."""
    from suppliers.models import SupplierRequestLetter
    qs = list(SupplierRequestLetter.objects.filter(request=req, user=user, sent_at=None)
              .select_related("recipient__supplier"))
    at = max((x.updated_at for x in qs), default=None)
    return {"letters": [_letter_item(x) for x in qs], "at": timezone.localtime(at).strftime("%d.%m.%Y %H:%M") if at
            else "", "wish": qs[0].wish if qs else ""}


def send_each(req, user, items, report=None) -> dict:
    """Надіслати складені (і, можливо, виправлені людиною) листи — кожен окремо, відповіддю в гілці свого
    постачальника (reply). → {sent: [назви], failed: [[назва, причина]]}."""
    rows = {r.pk: r for r in req.recipients.select_related("supplier", "contact")}
    sent, failed = [], []
    items = [it for it in items if isinstance(it, dict)][:EACH_MAX]
    if not items:
        raise TalkError("Немає листів для надсилання.")
    for i, it in enumerate(items, 1):
        rid = int(it["rid"]) if str(it.get("rid") or "").isdigit() else 0
        r = rows.get(rid)
        if r is None:
            failed.append(["?", "постачальника немає в цьому запиті"])
            continue
        if i > 1 and EACH_PAUSE:
            time.sleep(EACH_PAUSE)
        for attempt, wait in enumerate((0,) + tuple(EACH_RETRY)):
            if report:
                report(f"📤 {i} / {len(items)} · {r.supplier.name}" + (
                    f" · ⏳ сервер пошти просить зачекати — повтор через {wait} с" if wait else ""))
            if wait:
                time.sleep(wait)
            try:
                reply(req, r, user, str(it.get("to") or ""), str(it.get("cc") or ""), str(it.get("subject") or ""),
                      str(it.get("body") or ""),
                      reply_pk=it.get("reply") if str(it.get("reply") or "").isdigit() else None)
                sent.append(r.supplier.name)
                break
            except TalkError as e:
                err = str(e)
                if attempt < len(EACH_RETRY) and _TRANSIENT_RE.search(err):
                    continue                     # тимчасово — ще раз після паузи
                logger.warning("rfq each: %s → %s: %s", req.code, r.supplier.name, err)
                failed.append([r.supplier.name, err])
                from suppliers.models import SupplierRequestLetter
                SupplierRequestLetter.objects.filter(recipient=r, user=user, sent_at=None).update(error=err[:2000])
                break
    return {"sent": sent, "failed": failed}


# ── ↪ переслати колезі ──

def _rows(req, rids):
    from suppliers.services import rfq_compare
    t = rfq_compare.table(req)
    want = {int(x) for x in rids if str(x).isdigit()}
    rows = [row for row in t["rows"] if row["recipient"].pk in want][:FORWARD_MAX_ROWS]
    if not rows:
        raise TalkError("Виберіть постачальників у таблиці (галочки ліворуч).")
    return t, rows


def _files(rows) -> list:
    """Файли постачальників вибраних рядків (найновіші першими) → [{obj, name, size, attach}]: attach — влазить у
    rfq.ATTACH_MAX_MB разом (решта — лише назвами в листі)."""
    from suppliers.services import rfq
    limit, used, out = rfq.ATTACH_MAX_MB * 1024 * 1024, 0, []
    for row in rows:
        r = row["recipient"]
        for f in r.reply_files.order_by("-received_at", "-pk"):
            size = f.size or 0
            fits = used + size <= limit
            if fits:
                used += size
            out.append({"obj": f, "supplier": r.supplier.name, "name": f.name, "size": size, "attach": fits})
    return out


def forward_plan(req, rids, user) -> dict:
    from suppliers.services import rfq_replies
    _t, rows = _rows(req, rids)
    files = _files(rows)
    out = []
    for row in rows:
        items = letters(row["recipient"], user)
        out.append({"rid": row["recipient"].pk, "name": row["recipient"].supplier.name,
                    "letters": sum(1 for x in items if x["readable"]),
                    "hidden": sum(1 for x in items if not x["readable"]),
                    "files": sum(1 for f in files if f["obj"].recipient_id == row["recipient"].pk)})
    return {"rows": out, "files": [{"name": f"{f['supplier']} · {f['name']}", "size": rfq_replies.size_text(f["size"]),
                                    "attach": f["attach"]} for f in files],
            "subject": f"{req.code} · {req.title} — пропозиції: " + ", ".join(x["name"] for x in out)[:150],
            "attach_mb": round(sum(f["size"] for f in files if f["attach"]) / 1024 / 1024, 1)}


def _table_html(t, rows) -> str:
    from suppliers.services.rfq_compare import money
    home = t["home"]
    head = "".join(f"<th style='border:1px solid #ccc;padding:4px 6px'>{q} шт<br><small>{home}/шт з разовими · "
                   f"разом</small></th>" for q in t["qtys"])
    body = []
    for n, row in enumerate(rows, 1):
        r = row["recipient"]
        cells = []
        for c in row["cells"]:
            if c.get("missing") or not row["priced"]:
                cells.append("<td style='border:1px solid #ccc;padding:4px 6px;color:#888'>—</td>")
                continue
            eff = f"{c['eff_home_text']} {home}" if c.get("eff_home_text") else f"{c['eff_text']} {r.currency}"
            tot = f"{c['total_home_text']} {home}" if c.get("total_home_text") else f"{c['total_text']} {r.currency}"
            src = f"{c['unit_text']} {r.currency}/шт" + (f" + {c['one_time_text']} разово" if c["one_time_text"] else "")
            cells.append(f"<td style='border:1px solid #ccc;padding:4px 6px'><b>{escape(eff)}</b>"
                         f"{' 🥇' if c.get('best') else ''}<br><small>разом {escape(tot)}<br>{escape(src)}"
                         f"{(' · ' + str(c['lead']) + ' дн.') if c.get('lead') is not None else ''}</small></td>")
        info = []
        if r.lead_days is not None:
            info.append(f"строк {r.lead_days} дн.")
        if r.shipping is not None:
            info.append(f"доставка {money(r.shipping)} {r.currency}")
        if r.valid_until:
            info.append(f"діє до {r.valid_until:%d.%m.%Y}")
        if row["gaps"]:
            info.append("⚠️ неповна: " + "; ".join(row["gaps"]))
        summary = (r.ai or {}).get("summary") or ""
        body.append(f"<tr><td style='border:1px solid #ccc;padding:4px 6px;vertical-align:top'>{n}. "
                    f"<b>{escape(r.supplier.name)}</b>{' 🏆' if r.chosen else ''}<br><small>"
                    f"{escape(r.get_state_display())}<br>{escape(r.email)}</small></td>{''.join(cells)}"
                    f"<td style='border:1px solid #ccc;padding:4px 6px;vertical-align:top'><small>"
                    f"{escape(' · '.join(info))}{('<br>🧠 ' + escape(summary)) if summary else ''}</small></td></tr>")
    return ("<table style='border-collapse:collapse;font-size:13px'><tr><th style='border:1px solid #ccc;padding:4px "
            f"6px'>Постачальник</th>{head}<th style='border:1px solid #ccc;padding:4px 6px'>Умови</th></tr>"
            + "".join(body) + "</table>")


def forward(req, rids, user, to, cc, subject, note, parts) -> dict:
    """Один лист колезі: примітка, таблиця вибраних пропозицій, листування кожного (лише ваші скриньки), файли."""
    from email_assistant.smtp_client import SMTPClient
    from suppliers.models import SupplierEvent
    from suppliers.services import rfq
    to_list, cc_list = rfq.addresses(to), rfq.addresses(cc)
    if not to_list:
        raise TalkError("Кому переслати — вкажіть адресу колеги.")
    t, rows = _rows(req, rids)
    account = rfq.account_for(user, req)
    if account is None:
        raise TalkError("У вас немає активної поштової скриньки в Minerva.")
    parts = set(parts or ())
    html = [f"<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px'>"]
    text = []
    if (note or "").strip():
        html.append(f"<p>{escape(note.strip()).replace(chr(10), '<br>')}</p>")
        text.append(note.strip())
    html.append(f"<p><b>{escape(req.code)} · {escape(req.title)}</b><br><small>Кількості: "
                f"{escape(req.quantities or '—')}" + (f" · надіслано {req.sent_at:%d.%m.%Y}" if req.sent_at else "")
                + "</small></p>")
    text.append(f"{req.code} · {req.title}\nКількості: {req.quantities or '—'}")
    if "table" in parts:
        html.append(_table_html(t, rows))
        for n, row in enumerate(rows, 1):
            r = row["recipient"]
            cells = "; ".join(f"{c['qty']} шт: {c.get('eff_home_text') or c['eff_text']} "
                              f"{t['home'] if c.get('eff_home_text') else r.currency}/шт"
                              for c in row["cells"] if row["priced"] and not c.get("missing"))
            text.append(f"{n}. {r.supplier.name} — {r.get_state_display()}" + (f": {cells}" if cells else ""))
    hidden = 0
    if "mail" in parts:
        for row in rows:
            r = row["recipient"]
            items = letters(r, user)
            hidden += sum(1 for x in items if not x["readable"])
            mine = [x for x in items if x["readable"]][-FORWARD_MAILS:]
            if not mine:
                continue
            html.append(f"<h3 style='margin:18px 0 6px'>✉️ {escape(r.supplier.name)}</h3>")
            text.append(f"\n===== ✉️ {r.supplier.name} =====")
            for x in mine:
                head = (f"{'📤' if x['ours'] else '📥'} {x['at']:%d.%m.%Y %H:%M} · {x['who']} <{x['email']}>"
                        if x["at"] else x["who"])
                files = (" · 📎 " + ", ".join(f["name"] for f in x["files"])) if x["files"] else ""
                html.append(f"<div style='margin:8px 0;padding:6px 10px;border-left:3px solid "
                            f"{'#5b8def' if x['ours'] else '#4caf50'};background:#f7f7f7'><small>{escape(head)}"
                            f"{escape(files)}</small><div><b>{escape(x['subject'])}</b></div>"
                            f"<div style='white-space:pre-wrap'>{escape(x['text'])}</div></div>")
                text.append(f"\n{head}{files}\n{x['subject']}\n{x['text']}")
    files = _files(rows) if "files" in parts else []
    attach, blobs = [f for f in files if f["attach"]], []
    for f in attach:
        try:
            with f["obj"].file.open("rb") as fh:
                blobs.append((f"{f['supplier'][:30]} - {f['name']}", fh.read()))
        except OSError:
            pass
    left = [f for f in files if not f["attach"]]
    if left:
        names = ", ".join(f"{f['supplier']} · {f['name']}" for f in left)
        html.append(f"<p><small>📎 Не вкладено (разом понад {rfq.ATTACH_MAX_MB} МБ): {escape(names)} — є в Minerva, "
                    f"запит {escape(req.code)}.</small></p>")
        text.append(f"\n📎 Не вкладено (разом понад {rfq.ATTACH_MAX_MB} МБ): {names}")
    if hidden:
        html.append(f"<p><small>🔒 Ще листів у скриньках колег: {hidden} — їх тут немає.</small></p>")
    html.append("</div>")
    attachments = rfq.as_attachments(blobs) if blobs else []
    subject = (subject or "").strip() or forward_plan(req, rids, user)["subject"]
    body_text, _html = rfq.letter(account, "\n".join(text))
    sig = rfq.signature(account)
    sig_html = (f"<div>-- <br>{sig['html']}</div>" if sig["html"] else
                f"<div>-- <br>{escape(sig['text']).replace(chr(10), '<br>')}</div>" if sig["text"] else "")
    full_html = "".join(html[:-1]) + sig_html + "</div>"
    try:
        result = SMTPClient(account).send(to_emails=to_list, cc_emails=cc_list, subject=subject[:250],
                                          body_text=body_text, body_html=full_html, attachments=attachments,
                                          signature=False)
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "error": str(e)}
    if not result.get("ok"):
        raise TalkError("Не надіслано: " + str(result.get("error") or "помилка SMTP")[:400])
    try:
        from django.db import transaction
        with transaction.atomic():
            rfq.store_sent(account, result, to_list, cc_list, subject[:250], body_text, attachments, body_html=full_html)
    except Exception:  # noqa: BLE001
        logger.warning("rfq talk: store forward failed", exc_info=True)
    for row in rows:
        SupplierEvent.objects.create(supplier=row["recipient"].supplier, request=req, kind=SupplierEvent.MAIL,
                                     user=user, text=f"↪ Переслано колезі → {', '.join(to_list)}")
    return {"ok": True, "to": to_list, "rows": len(rows), "files": len(blobs), "left": len(left), "hidden": hidden}


# ── ↪ «📨 Листи як є» — кожен лист вкладенням .eml, як на сервері пошти ──

def _short(name) -> str:
    """Початок назви постачальника для назви файлу: «HongRong (shenzhen)» з «HongRong (shenzhen) Electronics…»."""
    out = ""
    for word in str(name or "").split():
        if out and len(out) + 1 + len(word) > 20:
            break
        out = f"{out} {word}".strip()
    return out[:24]


def _mid(m) -> str:
    return "".join((m.message_id or "").split()).lower()


def _originals_items(rows, user):
    """Що піде: [{kind mail | file, r, msg | obj, size}] — листи кожного рядка за датою (лише ваші скриньки; лист,
    що є в кількох рядках або у двох скриньках, — раз), далі файли постачальника, яких немає в цих листах (додані
    вручну, зі скриньок колег). → (items, {rid: листів у скриньках колег})."""
    from email_assistant import originals
    from suppliers.services import rfq_replies
    seen_pk, seen_id, items, hidden = set(), set(), [], {}
    for row in rows:
        r = row["recipient"]
        mails, mine = [], set()
        for m in rfq_replies.mail(r):
            if not rfq_replies.can_read(m, user):
                hidden[r.pk] = hidden.get(r.pk, 0) + 1
                continue
            key = _mid(m)
            if m.pk in seen_pk or (key and (key in seen_id or key in mine)):
                continue
            mails.append(m)
            if key:
                mine.add(key)
        for m in mails[-ORIGINALS_MAX:]:
            seen_pk.add(m.pk)
            if _mid(m):
                seen_id.add(_mid(m))
            items.append({"kind": "mail", "r": r, "msg": m, "size": originals.estimate(m)})
        for f in r.reply_files.order_by("received_at", "pk"):
            if f.file and f.message_id not in seen_pk:
                items.append({"kind": "file", "r": r, "obj": f, "size": f.size or 0})
    return items, hidden


def _pack(items, limit) -> list:
    """Вкладення по листах колезі, кожен до limit байт, у тому самому порядку; більше за limit — окремим листом."""
    parts, cur, used = [], [], 0
    for it in items:
        if cur and used + it["size"] > limit:
            parts.append(cur)
            cur, used = [], 0
        cur.append(it)
        used += it["size"]
    if cur:
        parts.append(cur)
    return parts


def _too_big(it, limit) -> bool:
    """Файл постачальника понад ліміт не вкладається (він є в Minerva); лист — іде окремим листом колезі."""
    return it["kind"] == "file" and it["size"] > limit


def originals_plan(req, rids, user) -> dict:
    """План «📨 Листи як є» без сервера пошти: листів і ≈ МБ від кожного, скількома листами колезі піде."""
    from email_assistant import originals
    from suppliers.services import rfq
    _t, rows = _rows(req, rids)
    items, hidden = _originals_items(rows, user)
    limit = rfq.ATTACH_MAX_MB * 1024 * 1024
    left = [it for it in items if _too_big(it, limit)]
    items = [it for it in items if not _too_big(it, limit)]
    out = []
    for row in rows:
        r = row["recipient"]
        mine = [it for it in items if it["r"].pk == r.pk]
        out.append({"rid": r.pk, "name": r.supplier.name, "hidden": hidden.get(r.pk, 0),
                    "letters": sum(1 for it in mine if it["kind"] == "mail"),
                    "files": sum(1 for it in mine if it["kind"] == "file"),
                    "mb": round(sum(it["size"] for it in mine) / 1024 / 1024, 1)})
    return {"rows": out, "parts": len(_pack(items, limit)), "parts_max": ORIGINALS_PARTS_MAX,
            "mb": round(sum(it["size"] for it in items) / 1024 / 1024, 1), "limit_mb": rfq.ATTACH_MAX_MB,
            "local": sum(1 for it in items if it["kind"] == "mail" and not originals.on_server(it["msg"])),
            "big": [it["msg"].subject or "—" for it in items if it["kind"] == "mail" and it["size"] > limit],
            "left": [f"{it['r'].supplier.name} · {it['obj'].name}" for it in left]}


def _originals_body(req, t, rows, part, k, n, note, table, own, got, hidden, left) -> tuple:
    """Текст листа колезі (HTML-шматки, рядки тексту): коментар, запит, таблиця — у першому; що в цьому листі
    у вкладенні; у останньому — що не вкладено й скільки листів у скриньках колег."""
    from suppliers.services import rfq, rfq_replies
    html, text = ["<div style='font-family:Arial,Helvetica,sans-serif;font-size:14px'>"], []
    if k == 1:
        if (note or "").strip():
            html.append(f"<p>{escape(note.strip()).replace(chr(10), '<br>')}</p>")
            text.append(note.strip())
        html.append(f"<p><b>{escape(req.code)} · {escape(req.title)}</b><br><small>Кількості: "
                    f"{escape(req.quantities or '—')}"
                    + (f" · надіслано {req.sent_at:%d.%m.%Y}" if req.sent_at else "") + "</small></p>")
        text.append(f"{req.code} · {req.title}\nКількості: {req.quantities or '—'}")
        if table:
            html.append(_table_html(t, rows))
    if n > 1:
        html.append(f"<p><b>Лист {k} з {n}</b> — листів багато, тож пересилання розбито на частини "
                    f"(кожна до {rfq.ATTACH_MAX_MB} МБ).</p>")
        text.append(f"\nЛист {k} з {n} (кожна частина до {rfq.ATTACH_MAX_MB} МБ)")
    mails = sum(1 for it in part if it["kind"] == "mail")
    html.append(f"<p>📨 У вкладенні листів: {mails} — кожен відкривається як звичайний лист, з текстом, картинками "
                f"і файлами.</p>")
    text.append(f"\n📨 У вкладенні листів: {mails} — кожен відкривається як звичайний лист.")
    supplier = None
    for it in part:
        r = it["r"]
        if r.pk != supplier:
            if supplier is not None:
                html.append("</ul>")
            supplier = r.pk
            html.append(f"<div style='margin-top:10px'><b>{escape(r.supplier.name)}</b></div>"
                        f"<ul style='margin:4px 0'>")
            text.append(f"\n== {r.supplier.name} ==")
        if it["kind"] == "file":
            line = (f"   📎 файл цього листа (збережений у запиті): {it['obj'].name}" if it.get("of_mail")
                    else f"📎 файл постачальника: {it['obj'].name}")
        else:
            m, o = it["msg"], got.get(it["msg"].pk)
            arrow = "📥" if rfq_replies.theirs(m, own) else "📤"
            when = f"{timezone.localtime(m.sent_at):%d.%m.%Y %H:%M} · " if m.sent_at else ""
            files = [a.get("name") for a in m.attachments or [] if isinstance(a, dict) and a.get("name")
                     and not a.get("is_inline")]
            line = f"{arrow} {when}{m.from_name or m.from_email} · {m.subject or '—'}"
            if files:
                line += " · 📎 " + ", ".join(files)
            if o is not None and o.rebuilt:
                line += (" · ⚠️ копії немає на сервері пошти — зібрано з Minerva"
                         + (", без файлів: " + ", ".join(o.missing) if o.missing else ""))
        html.append(f"<li>{escape(line)}</li>")
        text.append(f"- {line}")
    if supplier is not None:
        html.append("</ul>")
    if k == n:
        if left:
            names = ", ".join(left)
            html.append(f"<p><small>📎 Не вкладено (кожен понад {rfq.ATTACH_MAX_MB} МБ): {escape(names)} — є в "
                        f"Minerva, запит {escape(req.code)}.</small></p>")
            text.append(f"\n📎 Не вкладено (кожен понад {rfq.ATTACH_MAX_MB} МБ): {names}")
        if hidden:
            html.append(f"<p><small>🔒 Ще листів у скриньках колег: {hidden} — їх тут немає.</small></p>")
            text.append(f"🔒 Ще листів у скриньках колег: {hidden} — їх тут немає.")
    return html, text


def forward_originals(req, rids, user, to, cc, subject, note, table=True, report=None) -> dict:
    """«📨 Листи як є»: листування вибраних постачальників колезі — кожен лист вкладенням .eml (оригінал із сервера
    пошти), файли без листа — звичайними вкладеннями; понад rfq.ATTACH_MAX_MB — кількома листами «(1/2)»."""
    import mimetypes
    from email_assistant import originals
    from email_assistant.smtp_client import SMTPClient
    from suppliers.models import SupplierEvent
    from suppliers.services import rfq, rfq_replies
    to_list, cc_list = rfq.addresses(to), rfq.addresses(cc)
    if not to_list:
        raise TalkError("Кому переслати — вкажіть адресу колеги.")
    t, rows = _rows(req, rids)
    account = rfq.account_for(user, req)
    if account is None:
        raise TalkError("У вас немає активної поштової скриньки в Minerva.")
    items, hidden = _originals_items(rows, user)
    limit = rfq.ATTACH_MAX_MB * 1024 * 1024
    left = [f"{it['r'].supplier.name} · {it['obj'].name}" for it in items if _too_big(it, limit)]
    items = [it for it in items if not _too_big(it, limit)]
    if not items:
        raise TalkError("Немає листів, які можна переслати: листування з цими постачальниками — у скриньках колег "
                        "або його ще не було.")
    got = originals.fetch([it["msg"] for it in items if it["kind"] == "mail"], report)
    ready, taken = [], set()

    def add_file(it, obj, of_mail=False):
        r = it["r"]
        try:
            with obj.file.open("rb") as fh:
                data = fh.read()
        except (OSError, ValueError):
            left.append(f"{r.supplier.name} · {obj.name}")
            return False
        ready.append({**it, "kind": "file", "obj": obj, "of_mail": of_mail, "size": len(data), "att": {
            "name": f"{_short(r.supplier.name)} - {obj.name}", "content": data,
            "content_type": mimetypes.guess_type(obj.name)[0] or "application/octet-stream"}})
        return True

    for it in items:
        r = it["r"]
        if it["kind"] == "file":
            if it["obj"].pk not in taken:
                add_file(it, it["obj"])
            continue
        m, o = it["msg"], got[it["msg"].pk]
        ready.append({**it, "size": len(o.data),
                      "att": originals.attachment(originals.filename(m, _short(r.supplier.name)), o.data)})
        if o.rebuilt and o.missing:
            # копії листа на сервері немає, а його файли вже в запиті (відповідь постачальника, файли самого
            # запиту) — ідуть поруч, звичайними вкладеннями; у листі колезі — під цим листом
            have = {f.name: f for f in list(req.files.all()) + list(r.reply_files.filter(message=m))
                    if f.file and f.size <= limit}
            found = [have[name] for name in dict.fromkeys(o.missing) if name in have]
            for f in found:
                if add_file(it, f, of_mail=True) and hasattr(f, "recipient_id"):
                    taken.add(f.pk)
            got[m.pk] = o._replace(missing=[x for x in o.missing if x not in {f.name for f in found}])
    parts = _pack(ready, limit)
    if not parts:
        raise TalkError("Немає листів, які можна переслати.")
    if len(parts) > ORIGINALS_PARTS_MAX:
        raise TalkError(f"Разом {sum(x['size'] for x in ready) / 1024 / 1024:.0f} МБ — це {len(parts)} листів "
                        f"колезі (за раз — до {ORIGINALS_PARTS_MAX}). Виберіть менше постачальників.")
    own = rfq_replies._own()
    base = ((subject or "").strip() or forward_plan(req, rids, user)["subject"])[:230]
    sig = rfq.signature(account)
    sig_html = (f"<div>-- <br>{sig['html']}</div>" if sig["html"] else
                f"<div>-- <br>{escape(sig['text']).replace(chr(10), '<br>')}</div>" if sig["text"] else "")
    n, total_hidden = len(parts), sum(hidden.values())
    for k, part in enumerate(parts, 1):
        if report:
            report(f"📤 Надсилаю колезі: {k} / {n}")
        html, text = _originals_body(req, t, rows, part, k, n, note, table, own, got, total_hidden, left)
        subj = base + (f" ({k}/{n})" if n > 1 else "")
        body_text, _html = rfq.letter(account, "\n".join(text))
        full_html = "".join(html) + sig_html + "</div>"
        attachments = [it["att"] for it in part]
        try:
            result = SMTPClient(account).send(to_emails=to_list, cc_emails=cc_list, subject=subj, body_text=body_text,
                                              body_html=full_html, attachments=attachments, signature=False)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": str(e)}
        if not result.get("ok"):
            done = f"Надіслано листів колезі: {k - 1} з {n}. " if n > 1 else ""
            raise TalkError(done + f"Лист {k} не надіслано: " + str(result.get("error") or "помилка SMTP")[:400])
        try:
            from django.db import transaction
            with transaction.atomic():
                rfq.store_sent(account, result, to_list, cc_list, subj, body_text, attachments, body_html=full_html,
                               keep_files=True)
        except Exception:  # noqa: BLE001
            logger.warning("rfq talk: store forward originals failed", exc_info=True)
        if k < n and EACH_PAUSE:
            time.sleep(EACH_PAUSE)
    mails = sum(1 for it in ready if it["kind"] == "mail")
    files = sum(1 for it in ready if it["kind"] == "file")
    for row in rows:
        r = row["recipient"]
        count = sum(1 for it in ready if it["r"].pk == r.pk and it["kind"] == "mail")
        SupplierEvent.objects.create(supplier=r.supplier, request=req, kind=SupplierEvent.MAIL, user=user,
                                     text=f"↪ Переслано колезі листи як є ({count}) → {', '.join(to_list)}")
    message = (f"✓ Переслано → {', '.join(to_list)}: листів {mails}" + (f", файлів {files}" if files else "")
               + (f" · {n} листами" if n > 1 else ""))
    return {"ok": True, "to": to_list, "letters": mails, "files": files, "parts": n, "left": len(left),
            "hidden": total_hidden, "rebuilt": sum(1 for o in got.values() if o.rebuilt), "message": message}
