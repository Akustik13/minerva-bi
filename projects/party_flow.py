"""🕒 «Як рухався проєкт» — діаграма по сторонах у часі (вкладка «🔗 Сторони» пульта, вигляд «🕒 Як рухалось»).

Рядки — сторони: замовник, ми, кожен постачальник / виробник (сторони проєкту, закупівлі, JLC, запити
постачальникам), логістика. Горизонталь — дати, зверху — фази плану. На рядку — події в їхні дати:
📝 Angebot / ✅ прийнято, 🛒 замовлення клієнта, 🚚 відправлення; 📨 запит постачальнику, 💶 / 📥 відповідь, ✅ обрано
(що доручили) / ✖ не взяли; 🛒 закупівля, ⏳ чекаємо, 📥 отримано; 🔧 JLC, 🚚 відвантажено, 📥 доставлено; 📌 моменти;
✉️ листування (скільки листів того дня, адреса чи домен сторони; тема — лише власнику скриньки).
Лише читання: факти — overview.collect (вже пораховані для пульта), запити — projects/sourcing.py.
"""
from collections import defaultdict
from datetime import date, timedelta
from html import escape

from django.utils import timezone

from .models import ProjectParty

W, LABEL, HEAD, ROW, PAD = 1000, 210, 54, 40, 14
PRIORITY = ["✅", "✖", "🛒", "💶", "📥", "📨", "🚚", "🔧", "📝", "⏳", "📌", "✉️"]
P = ProjectParty


def _day(value):
    if value is None:
        return None
    if hasattr(value, "hour"):
        return (timezone.localtime(value) if timezone.is_aware(value) else value).date()
    return value


def _fmt(d) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _cut(text, n) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class Rows:
    """Рядки діаграми: ключ → {name, icon, sub, order}; адреси й домени листування → ключ."""
    def __init__(self):
        self.rows, self.emails, self.domains = {}, {}, {}

    def add(self, key, name, icon, order, sub=""):
        r = self.rows.setdefault(key, {"key": key, "name": name, "icon": icon, "order": order, "sub": sub,
                                       "events": []})
        if sub and not r["sub"]:
            r["sub"] = sub
        return key

    def mail(self, key, *addresses):
        from email_assistant.crm_link import FREE_MAIL_DOMAINS
        for a in addresses:
            a = str(a or "").strip().lower()
            if "@" in a:
                self.emails.setdefault(a, key)
                dom = a.split("@", 1)[1]
                if dom not in FREE_MAIL_DOMAINS:
                    self.domains.setdefault(dom, key)
            elif "." in a and a not in FREE_MAIL_DOMAINS:
                self.domains.setdefault(a, key)

    def find(self, address):
        a = str(address or "").strip().lower()
        return self.emails.get(a) or (self.domains.get(a.split("@", 1)[1]) if "@" in a else None)


def _supplier_key(sup):
    return f"s{sup.pk}"


def collect(project, facts, viewer=None):
    """→ (rows: [ряд з events], phases: [(назва, початок, кінець)], dates: всі дати подій)."""
    from . import parties as pt
    from . import sourcing
    R = Rows()
    uid = getattr(viewer, "pk", None)
    company = pt.company_name()
    # замовник і ми — завжди
    cust = project.customer
    if cust is not None:
        from .portfolio import customer_name
        R.add("c", customer_name(cust) or str(cust), "👤", 0, "замовник")
        R.mail("c", cust.email)
    R.add("we", company, "🏢", 1, "ми")
    # сторони проєкту
    for p in project.parties.select_related("supplier"):
        if p.role == P.CUSTOMER:
            key = "c" if cust is not None else R.add(f"party{p.pk}", p.name, "👤", 0, "замовник")
        elif p.role == P.WE:
            key = "we"
        elif p.supplier_id:
            key = R.add(_supplier_key(p.supplier), p.supplier.name, pt.ROLE_ICON[p.role], 5, pt.ROLE_NAME[p.role])
        elif p.name.strip().upper().startswith("JLC"):
            key = R.add("jlc", "JLCPCB", "🏭", 4, "виробник")
        else:
            key = R.add(f"party{p.pk}", p.name, pt.ROLE_ICON[p.role], 6 if p.role == P.LOGISTICS else 5,
                        pt.ROLE_NAME[p.role])
        R.mail(key, p.email)
        if p.supplier_id:
            R.mail(key, p.supplier.email, *(d.strip() for d in (getattr(p.supplier, "domains", "") or "").split(",")))
    for part in project.participants.all():
        if part.role == "customer" and cust is not None:
            R.mail("c", part.email)

    def ev(key, day, icon, text, url="", cls=""):
        d = _day(day)
        if key in R.rows and d is not None:
            R.rows[key]["events"].append({"d": d, "icon": icon, "text": text, "url": url, "cls": cls})

    # клієнт: Angebote, замовлення, відправлення
    for o in facts.get("offers") or []:
        url = f"/admin/offers/offer/{o.pk}/change/"
        if o.sent_at:
            ev("c" if "c" in R.rows else "we", o.sent_at, "📝", f"Angebot {o.display_number} надіслано", url)
        if o.status == o.ACCEPTED and getattr(o, "decided_at", None):
            ev("c" if "c" in R.rows else "we", o.decided_at, "✅", f"Angebot {o.display_number} прийнято", url, "ok")
        elif o.status == o.REJECTED and getattr(o, "decided_at", None):
            ev("c" if "c" in R.rows else "we", o.decided_at, "✖", f"Angebot {o.display_number} відхилено", url, "off")
    for so in facts.get("orders") or []:
        ev("c" if "c" in R.rows else "we", so.order_date, "🛒", f"Замовлення {so.source}:{so.order_number}",
           f"/admin/sales/salesorder/{so.pk}/change/")
    # закупівлі
    for po in facts.get("purchases") or []:
        if not po.supplier_id:
            continue
        key = R.add(_supplier_key(po.supplier), po.supplier.name, "📦", 5, "постачальник")
        R.mail(key, po.supplier.email, *(d.strip() for d in (getattr(po.supplier, "domains", "") or "").split(",")))
        from .portfolio import po_label
        label = po_label(po)
        url = f"/admin/inventory/purchaseorder/{po.pk}/change/"
        ev(key, po.order_date, "🛒", label, url, "open" if po.status == po.Status.DRAFT else "")
        if po.received_date:
            ev(key, po.received_date, "📥", f"{po} отримано", url, "ok")
        elif po.expected_date and po.status in (po.Status.ORDERED, po.Status.PARTIAL):
            late = po.expected_date < timezone.localdate()
            ev(key, po.expected_date, "⏳", f"{po}: чекали {_fmt(po.expected_date)}" if late else
               f"{po}: очікується {_fmt(po.expected_date)}", url, "bad" if late else "open")
    # JLC
    for j in facts.get("jlc") or []:
        key = R.add("jlc", "JLCPCB", "🏭", 4, "виробник")
        num = j.jlc_order_number or j.jlc_order_id
        url = f"/admin/jlcpcb/jlcorder/{j.pk}/change/"
        ev(key, j.order_date, "🔧", f"JLC {num} замовлено", url)
        if getattr(j, "shipped_date", None):
            ev(key, j.shipped_date, "🚚", f"JLC {num} відвантажено", url)
        if getattr(j, "delivered_date", None):
            ev(key, j.delivered_date, "📥", f"JLC {num} доставлено", url, "ok")
    # відправлення клієнту
    for s in facts.get("shipments") or []:
        if getattr(s, "direction", "out") == "out":
            ev("c" if "c" in R.rows else "we", s.submitted_at or s.created_at, "🚚",
               f"Відправлення {s.tracking_number or '#%s' % s.pk}", f"/admin/shipping/shipment/{s.pk}/change/")
    # запити постачальникам
    for req in sourcing.requests(project):
        url = f"/admin/suppliers/supplierrequest/{req.pk}/work/"
        for r in req.recipients.all():
            sup = r.supplier
            o = sourcing.outcome(r)
            key = R.add(_supplier_key(sup), sup.name, "📦", 7 if o in sourcing.OFF else 5,
                        "запит постачальнику")
            R.mail(key, r.email, sup.email, *(d.strip() for d in (getattr(sup, "domains", "") or "").split(",")))
            if r.sent_at:
                ev(key, r.sent_at, "📨", f"{req.code} «{sourcing.what(req)}»: запит надіслано", url)
            if r.replied_at:
                ev(key, r.replied_at, "💶" if o in ("quoted", "chosen", "rejected") and r.price is not None else "📥",
                   sourcing.line(r, req) if o in ("quoted", "replied") else f"{req.code}: відповідь", url,
                   "warn")
            if o in ("chosen", "rejected"):
                when = req.closed_at or r.replied_at or r.sent_at or req.created_at
                ev(key, when, "✅" if o == "chosen" else "✖", sourcing.line(r, req), url,
                   "ok" if o == "chosen" else "off")
            elif o in ("declined", "silent"):
                ev(key, r.replied_at or req.closed_at or r.sent_at, "✖", sourcing.line(r, req), url, "off")
            R.rows[key]["outcome"] = R.rows[key].get("outcome") or o
            if o == "chosen":
                R.rows[key]["outcome"] = "chosen"
                R.rows[key]["sub"] = _cut("✅ " + (r.award or req.title), 34)
    # моменти — у рядок «ми»
    for h in facts.get("highlights") or []:
        ev("we", h.date, "📌", h.title, "")
    # листування: скільки листів того дня з кожною стороною (тема — лише власнику)
    per = defaultdict(list)
    for x in facts.get("messages") or []:
        m = x["msg"]
        if x["dir"] == "internal" or not m.sent_at:
            continue
        addrs = [m.from_email] if x["dir"] == "in" else list(m.to_emails or [])
        keys = {R.find(a) for a in addrs} - {None}
        for key in keys:
            per[(key, _day(m.sent_at))].append(m)
    for (key, d), msgs in per.items():
        own = [m for m in msgs if m.account_id and m.account.user_id == uid]
        lines = [f"✉️ {len(msgs)} лист(и) {_fmt(d)}"] + [f"«{_cut(m.subject or '(без теми)', 60)}»" for m in own[:4]]
        ev(key, d, "✉️", "\n".join(lines), f"/email/message/{own[0].pk}/" if len(msgs) == 1 and own else "", "mail")
    phases = []
    plan = facts.get("plan")
    if plan is not None and getattr(plan, "has_plan", False):
        for pc in plan.phases:
            if pc.start and pc.end:
                phases.append((((pc.phase.icon or "") + " " + pc.phase.name).strip() if pc.phase else "без фази",
                               pc.start, pc.end))
    rows = sorted((r for r in R.rows.values() if r["events"] or r["key"] in ("c", "we")),
                  key=lambda r: (r["order"], r.get("outcome") in ("rejected", "declined", "silent"), r["name"].lower()))
    return rows, phases


def render(project, facts, viewer=None, today=None) -> str:
    today = today or timezone.localdate()
    rows, phases = collect(project, facts, viewer)
    days = [e["d"] for r in rows for e in r["events"]] + [d for _, a, b in phases for d in (a, b)]
    if project.start_date:
        days.append(project.start_date)
    if not [r for r in rows if r["events"]]:
        return ""
    lo = min(days + [today]) - timedelta(days=3)
    hi = max(days + [today, project.due_date or today]) + timedelta(days=5)
    span = max((hi - lo).days, 14)
    ppd = (W - LABEL - PAD) / span

    def X(d):
        return LABEL + (d - lo).days * ppd + ppd / 2

    H = HEAD + len(rows) * ROW + 16
    out = [f'<svg class="pjf" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Як рухався проєкт: сторони в часі" xmlns="http://www.w3.org/2000/svg">']
    # фази плану — смуги зверху й фон
    for i, (name, a, b) in enumerate(phases):
        x1, x2 = X(a) - ppd / 2, X(b) + ppd / 2
        out.append(f'<rect x="{x1:.1f}" y="{HEAD - 6}" width="{max(x2 - x1, 2):.1f}" height="{H - HEAD - 10}" '
                   f'class="pjf-band pjf-b{i % 2}"/>')
        out.append(f'<rect x="{x1:.1f}" y="8" width="{max(x2 - x1, 2):.1f}" height="18" rx="4" class="pjf-ph pjf-p{i % 2}">'
                   f'<title>{escape(name)} · {_fmt(a)} – {_fmt(b)}</title></rect>')
        if x2 - x1 > 40:
            out.append(f'<text x="{x1 + 5:.1f}" y="21" class="pjf-pht">{escape(_cut(name, max(int((x2 - x1) / 7), 4)))}</text>')
    # місяці
    d = date(lo.year, lo.month, 1)
    while d <= hi:
        if d >= lo:
            x = X(d) - ppd / 2
            out.append(f'<line x1="{x:.1f}" y1="30" x2="{x:.1f}" y2="{H - 10}" class="pjf-mon"/>')
            out.append(f'<text x="{x + 3:.1f}" y="42" class="pjf-mt">{d:%m.%Y}</text>')
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    # рядки
    for i, r in enumerate(rows):
        y = HEAD + i * ROW
        cy = y + ROW / 2
        off = r.get("outcome") in ("rejected", "declined", "silent") and r["key"] not in ("c", "we")
        out.append(f'<g class="pjf-row{" pjf-off" if off else ""}{" pjf-chosen" if r.get("outcome") == "chosen" else ""}">')
        out.append(f'<rect x="0" y="{y}" width="{W}" height="{ROW}" class="pjf-rbg pjf-r{i % 2}"/>')
        out.append(f'<text x="10" y="{cy - 2:.1f}" class="pjf-name">{escape(r["icon"])} {escape(_cut(r["name"], 26))}</text>')
        if r["sub"]:
            out.append(f'<text x="28" y="{cy + 12:.1f}" class="pjf-sub">{escape(_cut(r["sub"], 30))}</text>')
        evs = sorted(r["events"], key=lambda e: e["d"])
        real = [e for e in evs if e["icon"] != "✉️"]
        if len(real) > 1:
            out.append(f'<line x1="{X(real[0]["d"]):.1f}" y1="{cy:.1f}" x2="{X(real[-1]["d"]):.1f}" y2="{cy:.1f}" '
                       f'class="pjf-line"/>')
        groups = defaultdict(list)
        for e in evs:
            groups[e["d"]].append(e)
        for d, es in sorted(groups.items()):
            es.sort(key=lambda e: PRIORITY.index(e["icon"]) if e["icon"] in PRIORITY else 99)
            top = es[0]
            x = X(d)
            tip = f"{_fmt(d)}\n" + "\n".join(e["text"] for e in es)
            url = next((e["url"] for e in es if e["url"]), "")
            mail_only = all(e["icon"] == "✉️" for e in es)
            body = (f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="4" class="pjf-mail"/>' if mail_only else
                    f'<circle cx="{x:.1f}" cy="{cy:.1f}" r="11" class="pjf-dot pjf-{top["cls"] or "n"}"/>'
                    f'<text x="{x:.1f}" y="{cy + 4.5:.1f}" class="pjf-ic" text-anchor="middle">{escape(top["icon"])}</text>')
            if len([e for e in es if e["icon"] != "✉️"]) > 1:
                body += f'<text x="{x + 9:.1f}" y="{cy - 8:.1f}" class="pjf-n">{len(es)}</text>'
            g = f'<g class="pjf-ev"><title>{escape(tip)}</title>{body}</g>'
            out.append(f'<a href="{escape(url)}" target="_blank">{g}</a>' if url else g)
        out.append("</g>")
    # сьогодні й строк клієнту
    xt = X(today)
    out.append(f'<line x1="{xt:.1f}" y1="30" x2="{xt:.1f}" y2="{H - 10}" class="pjf-today"><title>сьогодні {_fmt(today)}</title></line>')
    if project.due_date:
        xd = X(project.due_date)
        out.append(f'<line x1="{xd:.1f}" y1="30" x2="{xd:.1f}" y2="{H - 10}" class="pjf-due">'
                   f'<title>строк клієнту {_fmt(project.due_date)}</title></line>')
    out.append(f'<line x1="{LABEL - 6}" y1="{HEAD - 6}" x2="{LABEL - 6}" y2="{H - 10}" class="pjf-sep"/>')
    out.append("</svg>")
    return "".join(out)
