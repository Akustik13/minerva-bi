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
  compose_each / send_each  — ✉️ «Написати кожному»: вибраним постачальникам — кожному свій лист відповіддю в його
                              гілці (target: автор останнього листа, копія — лише адреса цієї ж фірми), з вашої
                              скриньки; текст — AI за побажанням людини (ai.each_letter, мовою листування, на ім'я)
                              або один шаблон ({greeting} {name} {company} {code} — fill); людина бачить і правит
                              кожен лист до надсилання, інші постачальники його не бачать.
"""
import hashlib
import logging
import re

from django.utils import timezone
from django.utils.html import escape

logger = logging.getLogger(__name__)

BRIEF_TTL = 30 * 86400
BRIEF_MAILS = 12             # найновіших листів у «🧭 Коротко»
LETTER_CHARS = 6000          # тексту одного листа у вікні / пересиланні
FORWARD_MAILS = 30           # листів одного постачальника в пересиланні
FORWARD_MAX_ROWS = 30
EACH_MAX = 30                # листів «✉️ Написати кожному» за раз


class TalkError(Exception):
    pass


# ── листування рядка ──

def letters(r, user) -> list:
    from suppliers.services import rfq_replies
    own = rfq_replies._own()
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
                       "size": rfq_replies.size_text(a.get("size"))}
                      for a in (m.attachments or []) if rfq_replies.wanted(a)],
        })
    return out


def _re(subject) -> str:
    s = (subject or "").strip()
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
    return {**got, "fresh": got.get("fp") == fingerprint(items), "at_dt": _when(got.get("at"))}


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
    value = {"data": data, "fp": fingerprint(items), "at": timezone.now().isoformat(), "model": ai.model_of(model)}
    cache.set(_key(user, r), value, BRIEF_TTL)
    return {**value, "fresh": True, "at_dt": _when(value["at"])}


def _offer(row, r) -> str:
    """Ціни постачальника з таблиці одним рядком (для AI) — «5 шт: 1,92 USD/шт; 2500 шт: …»."""
    if not row or not row["priced"]:
        return ""
    return "; ".join(f"{c['qty']} шт: {c['unit_text']} {r.currency}/шт" for c in row["cells"] if not c.get("missing"))


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
    subject = (subject or "").strip() or _re(prev.subject if prev else rfq.render(req, r)[0])
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
                item["body"] = ai.each_letter(req, r, mine[-BRIEF_MAILS:], _offer(row, r), wish, _person(r, items),
                                              user=user, model=model)["body"]
            except ai.AIError as e:
                item["error"] = str(e)[:300]
            if not item["body"]:
                item["error"] = item["error"] or "AI не склав лист — напишіть самі."
        out.append(item)
    return out


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
        if report:
            report(f"📤 {i} / {len(items)} · {r.supplier.name}")
        try:
            reply(req, r, user, str(it.get("to") or ""), str(it.get("cc") or ""), str(it.get("subject") or ""),
                  str(it.get("body") or ""), reply_pk=it.get("reply") if str(it.get("reply") or "").isdigit() else None)
            sent.append(r.supplier.name)
        except TalkError as e:
            failed.append([r.supplier.name, str(e)])
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
