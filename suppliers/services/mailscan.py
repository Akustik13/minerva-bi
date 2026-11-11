"""
suppliers/services/mailscan.py — ✉️ листування з постачальником за період «з … по …», без AI.

Лише листи ВЛАСНИХ скриньок людини (account__user) — і для адміністратора: пошта кожного своя.
Лист належить постачальнику, якщо його адреса / домен (email, контактні особи, сайт, «Поштові домени») серед
відправника, отримувачів чи копії, або в тексті згадано код закупівлі (PO-…), номер замовлення постачальника
чи трек-номер його закупівлі (переслане колегою).

→ гілки, листи вхідні / наші, швидкість відповіді (їхня на наш лист і наша на їхній — медіана в годинах),
листи без відповіді, групування за закупівлями (і що без закупівлі), документи-вкладення по групах, листи з
ознаками проблем (затримка, брак, рекламація… — за словами, з цитатою). Оцінку AI з тих самих листів робить
suppliers/services/ai.py (evaluate).
"""
import datetime
import re
from collections import defaultdict
from statistics import median

from django.db.models import Q
from django.utils import timezone

from suppliers.services import match

SCAN_MAX = 1500
PROBLEMS_MAX = 40
SNIPPET = 220
PROBLEM_RE = re.compile(
    r"\b(delay(?:ed)?|late(?:r)? (?:delivery|shipment)|postpone\w*|reschedul\w*|defect\w*|damaged|broken|"
    r"wrong (?:part|item|quantity|qty)|missing|shortage|complain\w*|claim|RMA|return(?:ed)? goods|rework|"
    r"quality (?:issue|problem)|not (?:in stock|available)|out of stock|cancel(?:led|ed)?|"
    r"verzöger\w*|verspät\w*|verschoben|defekt\w*|beschädigt\w*|falsch\w*|fehl(?:t|en|ende)\w*|reklamation\w*|"
    r"mangel\w*|rücksendung|nicht lieferbar|storniert|"
    r"затримк\w*|запізн\w*|брак\w*|пошкодж\w*|дефект\w*|невірн\w*|не прийш\w*|претензі\w*|рекламаці\w*|"
    r"повернен\w*|скасова\w*|задержк\w*|повреж\w*)\b", re.I)


def period(date_from=None, date_to=None, days=365):
    """Дати з форми (рядки YYYY-MM-DD або date) → (від, до) включно."""
    def parse(v):
        if isinstance(v, datetime.date):
            return v
        try:
            return datetime.date.fromisoformat(str(v or "")[:10])
        except ValueError:
            return None
    to = parse(date_to) or timezone.localdate()
    frm = parse(date_from) or to - datetime.timedelta(days=days)
    return (frm, to) if frm <= to else (to, frm)


def po_tokens(supplier) -> dict:
    """{токен: закупівля} — код PO, номер замовлення постачальника, трек-номер (≥ 5 символів)."""
    from inventory.models import PurchaseOrder
    out = {}
    for po in PurchaseOrder.objects.filter(supplier=supplier).only("pk", "code", "supplier_order_no",
                                                                    "tracking_number", "order_date", "status"):
        for t in (po.code, po.supplier_order_no, po.tracking_number):
            t = (t or "").strip()
            if len(t) >= 5:
                out.setdefault(t, po)
    return out


def messages(supplier, user, date_from, date_to):
    """Листи власних скриньок людини, що стосуються постачальника, за період (старі першими)."""
    from email_assistant.models import EmailMessage
    emails, domains = match.keys(supplier)
    tokens = po_tokens(supplier)
    cond = Q(pk__in=[])
    for e in emails:
        cond |= Q(from_email__iexact=e) | Q(to_emails__icontains=e) | Q(cc_emails__icontains=e)
    for d in domains:
        cond |= Q(from_email__iendswith="@" + d) | Q(from_email__iendswith="." + d) \
            | Q(to_emails__icontains="@" + d) | Q(cc_emails__icontains="@" + d)
    for t in tokens:
        cond |= Q(subject__icontains=t) | Q(body_text__icontains=t)
    start = timezone.make_aware(datetime.datetime.combine(date_from, datetime.time.min))
    end = timezone.make_aware(datetime.datetime.combine(date_to + datetime.timedelta(days=1), datetime.time.min))
    qs = (EmailMessage.objects.filter(account__user=user, is_deleted=False, is_spam=False,
                                      sent_at__gte=start, sent_at__lt=end)
          .exclude(folder__in=[EmailMessage.FOLDER_DRAFT, EmailMessage.FOLDER_SPAM])
          .filter(cond).select_related("thread", "account").order_by("sent_at", "pk"))
    seen, out = set(), []
    for m in qs[:SCAN_MAX]:
        key = m.message_id or f"pk:{m.pk}"     # копія з «Надісланих» і запис Minerva — один лист
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out, emails, domains, tokens


def _is_theirs(m, emails, domains) -> bool:
    frm = match.addr(m.from_email)
    return frm in emails or match.domain_matches(frm.split("@", 1)[1] if frm else "", domains)


def _documents(m) -> list:
    from inventory.services.po_documents import _documents as docs
    return [{"index": i, "name": a.get("name") or "document", "size": a.get("size") or 0} for i, a in docs(m)]


def _snippet(text, m) -> str:
    start = max(0, m.start() - SNIPPET // 2)
    piece = " ".join(text[start:start + SNIPPET].split())
    return ("…" if start else "") + piece + "…"


def scan(supplier, user, date_from=None, date_to=None) -> dict:
    from email_assistant.crm_link import is_own, own_addresses
    from email_assistant.quotes import new_text
    date_from, date_to = period(date_from, date_to)
    msgs, emails, domains, tokens = messages(supplier, user, date_from, date_to)
    own = own_addresses()
    threads = defaultdict(list)
    groups = {}
    problems = []
    incoming = outgoing = 0
    for m in msgs:
        theirs = _is_theirs(m, emails, domains)
        ours = not theirs and (m.folder == m.FOLDER_SENT or is_own(m.from_email, own))
        m.theirs, m.ours = theirs, ours
        incoming += theirs
        outgoing += ours
        threads[m.thread_id or f"m{m.pk}"].append(m)
        text = f"{m.subject or ''}\n{m.body_text or ''}"
        po = next((po for t, po in tokens.items() if t.lower() in text.lower()), None)
        gkey = f"po:{po.pk}" if po else "none"
        g = groups.setdefault(gkey, {"po": po, "messages": [], "documents": []})
        g["messages"].append(m)
        for d in _documents(m):
            g["documents"].append({**d, "message": m})
        fresh = new_text(m.body_text or "")
        hit = PROBLEM_RE.search(f"{m.subject or ''}\n{fresh}")
        if hit and len(problems) < PROBLEMS_MAX:
            problems.append({"message": m, "word": hit.group(0), "snippet": _snippet(f"{m.subject or ''}\n{fresh}", hit),
                             "theirs": theirs})
    their_h, our_h, waiting_us, waiting_them = [], [], [], []
    for items in threads.values():
        items.sort(key=lambda x: (x.sent_at or timezone.now(), x.pk))
        for prev, nxt in zip(items, items[1:]):
            if not (prev.sent_at and nxt.sent_at) or nxt.sent_at < prev.sent_at:
                continue
            hours = (nxt.sent_at - prev.sent_at).total_seconds() / 3600
            if prev.ours and nxt.theirs:
                their_h.append(hours)
            elif prev.theirs and nxt.ours:
                our_h.append(hours)
        last = items[-1]
        if last.theirs:
            waiting_us.append(last)
        elif last.ours and last.sent_at and last.sent_at < timezone.now() - datetime.timedelta(days=3):
            waiting_them.append(last)
    po_groups = sorted((g for k, g in groups.items() if k != "none"),
                       key=lambda g: g["po"].order_date or datetime.date.min, reverse=True)
    if "none" in groups:
        po_groups.append(groups["none"])
    return {
        "date_from": date_from, "date_to": date_to, "emails": sorted(emails), "domains": sorted(domains),
        "messages": len(msgs), "incoming": incoming, "outgoing": outgoing, "threads": len(threads),
        "their_reply_hours": round(median(their_h), 1) if their_h else None,
        "their_reply_fast": round(100 * sum(h <= 24 for h in their_h) / len(their_h)) if their_h else None,
        "our_reply_hours": round(median(our_h), 1) if our_h else None,
        "pairs": len(their_h),
        "waiting_us": waiting_us[-10:][::-1], "waiting_them": waiting_them[-10:][::-1],
        "groups": po_groups, "problems": problems[::-1], "documents": sum(len(g["documents"]) for g in po_groups),
        "first": msgs[0].sent_at if msgs else None, "last": msgs[-1].sent_at if msgs else None,
        "truncated": len(msgs) >= SCAN_MAX,
    }


def summary(result: dict) -> dict:
    """Те, що зберігається в SupplierAnalysis.result (без об'єктів): числа й короткі списки."""
    return {
        "messages": result["messages"], "incoming": result["incoming"], "outgoing": result["outgoing"],
        "threads": result["threads"], "their_reply_hours": result["their_reply_hours"],
        "their_reply_fast": result["their_reply_fast"], "our_reply_hours": result["our_reply_hours"],
        "problems": [{"pk": p["message"].pk, "word": p["word"]} for p in result["problems"]],
        "documents": result["documents"],
        "orders": [g["po"].code for g in result["groups"] if g["po"] is not None][:30],
    }
