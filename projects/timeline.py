"""Шкала проєкту для пульта (projects/cockpit.py): події з усіх джерел на одній осі часу → SVG без бібліотек.

Доріжки (порожні не показуються): 📌 важливі моменти, ✉️ листування (точки по днях: від клієнта / наші, кружечок —
лист без нашої відповіді), 📝 Angebote й документи бази знань, 🛒 замовлення й відправлення, 🏭 закупівлі, JLC і посилки
до нас, 📅 терміни й зустрічі з листів проєкту, ⏱ години askDANTE по тижнях. Лінії: сьогодні, старт, строк клієнту.

Пошта й календар — особисті: тема листа чи назва події видна лише власнику скриньки / календаря, решті — кількість
листів «з інших скриньок»; події чужих календарів не показуються. Кольори — змінні тем Minerva (CSS у change_form).
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone
from django.utils.html import escape

from .models import ProjectHighlight

LANES = [
    ("moments", "📌 Важливі моменти"),
    ("mail", "✉️ Листування"),
    ("docs", "📝 Angebote й документи"),
    ("orders", "🛒 Замовлення й доставка"),
    ("supply", "🏭 Закупівлі й виробництво"),
    ("calendar", "📅 Терміни й зустрічі"),
    ("hours", "⏱ Час askDANTE"),
]
RANGES = {"all": None, "3": 3, "6": 6, "12": 12}
W, LEFT, RIGHT = 1000, 156, 12
HEAD, ROW_H, LANE_PAD, MAX_ROWS = 28, 18, 6, 3
MAIL_H, HOURS_H = 36, 42
CHAR_W = 6.0                 # середня ширина символу підпису (11 px)
LABEL_MAX = 38
FUTURE_DAYS = 21             # праворуч від «сьогодні», якщо строку клієнту немає
MAX_FUTURE_DAYS = 200        # далі в майбутнє шкала не тягнеться (крім строку клієнту)
MIN_DAYS = 35
MAIL_LIST = 12               # листів дня в меню точки


def _d(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def _fmt(value) -> str:
    d = _d(value)
    return d.strftime("%d.%m.%Y") if d else ""


def _cut(text, n=LABEL_MAX) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _n(v) -> str:
    s = f"{v:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _owner(message):
    return message.account.user_id if message is not None and message.account_id else None


# ── Події ────────────────────────────────────────────────────────────────────

def events(project, facts, viewer, today=None) -> list:
    """Події шкали: {lane, shape: flag | dot | point | diamond | bar | hours, start, end, label, tip, url, cls, …}."""
    today = today or timezone.localdate()
    uid = getattr(viewer, "pk", None)
    out = []

    def ev(lane, shape, start, label="", tip="", url="", cls="", end=None, **extra):
        start = _d(start)
        if start is None:
            return
        end = _d(end)
        if end is not None and end < start:
            end = start
        out.append({"lane": lane, "shape": shape, "start": start, "end": end, "label": _cut(label), "tip": tip,
                    "url": url, "cls": cls, **extra})

    for h in facts.get("highlights", []):
        own = h.message_id and _owner(h.message) == uid
        tip = f"📌 {_fmt(h.date)} · {h.get_kind_display()} · {h.title}" + (f"\n«{_cut(h.quote, 240)}»" if h.quote else "")
        cls = {ProjectHighlight.PROBLEM: "bad", ProjectHighlight.APPROVAL: "ok"}.get(h.kind, "")
        ev("moments", "flag", h.date, h.title, tip, f"/email/message/{h.message_id}/" if own else "", cls, hid=h.pk)

    _mail_events(ev, facts, uid)
    _doc_events(ev, project, facts, viewer)
    _order_events(ev, facts, today)
    _supply_events(ev, facts, today)

    now = timezone.now()
    for e in facts.get("deadlines", []):
        if e.user_id != uid:            # календар особистий: чужі події не показуються
            continue
        meeting = e.event_type == "meeting"
        cls = "ok" if e.is_done else ("bad" if e.start_at < now and not meeting else "warn")
        ev("calendar", "diamond", e.start_at, f"{'🤝' if meeting else '⏰'} {e.title}",
           f"{'🤝' if meeting else '⏰'} {_fmt(e.start_at)} · {e.title}{' · виконано' if e.is_done else ''}",
           f"/email/message/{e.email_message_id}/" if e.email_message_id else "", cls)

    out.extend(_hours(project, viewer))
    return out


def _mail_events(ev, facts, uid):
    days = defaultdict(lambda: {"in": [], "out": []})
    for x in facts.get("messages", []):
        if x["dir"] == "internal":
            continue
        d = _d(x["msg"].sent_at)
        if d is not None:
            days[d][x["dir"]].append(x["msg"])
    waiting = {m.pk for m in facts.get("waiting_us", [])}
    for d, by in days.items():
        for direction, msgs in by.items():
            if not msgs:
                continue
            own = [m for m in msgs if _owner(m) == uid]
            other = len(msgs) - len(own)
            head = f"{'📥 від клієнта' if direction == 'in' else '📤 наші'} · {_fmt(d)} · {len(msgs)}"
            lines = [head] + [f"«{_cut(m.subject or '(без теми)', 70)}» — "
                              f"{(m.from_name or m.from_email) if direction == 'in' else ', '.join((m.to_emails or [])[:2])}"
                              for m in own[:8]]
            if other:
                lines.append(f"ще {other} з інших скриньок")
            mails = [{"pk": m.pk, "s": _cut(m.subject or "(без теми)", 70), "d": d.isoformat()} for m in own[:MAIL_LIST]]
            ev("mail", "dot", d, "", "\n".join(lines),
               f"/email/message/{own[0].pk}/" if len(msgs) == 1 and own else "", direction,
               count=len(msgs), mails=mails, ring=any(m.pk in waiting for m in msgs))


def _doc_events(ev, project, facts, viewer):
    status_text = {"sent": "надіслано", "accepted": "прийнято", "rejected": "відхилено", "expired": "прострочено",
                   "cancelled": "скасовано"}
    for o in facts.get("offers", []):
        url = f"/admin/offers/offer/{o.pk}/change/"
        name = f"Angebot {o.display_number}"
        if not o.sent_at:
            ev("docs", "point", o.created_at, f"{name} · {o.get_status_display()}",
               f"📝 {name} · створено {_fmt(o.created_at)} · {o.get_status_display()}", url, "plan")
            continue
        decided = _d(o.decided_at) if o.status in (o.ACCEPTED, o.REJECTED) else None
        end = decided or (o.valid_until if o.status == o.SENT else None)
        cls = {"accepted": "ok", "rejected": "bad", "expired": "bad", "cancelled": "plan"}.get(o.status, "open")
        tip = (f"📝 {name} · надіслано {_fmt(o.sent_at)} · {o.get_status_display()}"
               + (f" · діє до {_fmt(o.valid_until)}" if o.valid_until else ""))
        ev("docs", "bar" if end and end > _d(o.sent_at) else "point", o.sent_at,
           f"{name} · {status_text.get(o.status, o.get_status_display())}", tip, url, cls, end=end)
    if viewer is None:
        return
    try:
        from rag_assistant import knowledge_items
        items = knowledge_items.items_for(viewer, "project", project, limit=40)
    except Exception:  # noqa: BLE001 — шкала без документів краще, ніж жодної
        return
    for item in items:
        ev("docs", "point", item.created_at, f"📎 {item.title}",
           f"📎 {item.title} · додано {_fmt(item.created_at)}", f"/rag/knowledge/{item.pk}/", "doc")


def _order_events(ev, facts, today):
    from shipping.models import Shipment
    for o in facts.get("orders", []):
        done = o.status in ("shipped", "delivered")
        end = _d(o.delivered_at) or _d(o.shipped_at) or (o.shipping_deadline if not done else None)
        if o.status == "cancelled":
            cls = "plan"
        elif done:
            cls = "ok"
        elif o.shipping_deadline and o.shipping_deadline < today:
            cls = "bad"
        else:
            cls = "open"
        tip = (f"🛒 {o.source}:{o.order_number} · {o.get_status_display()} · від {_fmt(o.order_date)}"
               + (f" · відправити до {_fmt(o.shipping_deadline)}" if o.shipping_deadline else "")
               + (f" · відправлено {_fmt(o.shipped_at)}" if o.shipped_at else "")
               + (f" · доставлено {_fmt(o.delivered_at)}" if o.delivered_at else ""))
        ev("orders", "bar" if end else "point", o.order_date or o.shipped_at,
           f"🛒 {o.source}:{o.order_number} · {o.get_status_display()}", tip,
           f"/admin/sales/salesorder/{o.pk}/change/", cls, end=end)
    for s in facts.get("shipments", []):
        incoming = s.direction == Shipment.IN
        start = s.submitted_at or s.created_at
        end = _d(s.delivered_at) or s.eta_to or s.carrier_eta
        if s.status == Shipment.Status.DELIVERED:
            cls = "ok"
        elif s.status == Shipment.Status.ERROR:
            cls = "bad"
        elif s.carrier_delayed or (end and end < today):
            cls = "warn"
        elif s.status == Shipment.Status.DRAFT:
            cls = "plan"
        else:
            cls = "open"
        carrier = s.carrier.name if s.carrier_id else ""
        name = f"{'📥' if incoming else '🚚'} {carrier} {s.tracking_number or f'#{s.pk}'}".replace("  ", " ")
        ev("supply" if incoming else "orders", "bar" if end else "point", start, f"{name} · {s.get_status_display()}",
           f"{name} · {s.get_status_display()} · {_fmt(start)}" + (f" → {_fmt(end)}" if end else ""),
           f"/admin/shipping/shipment/{s.pk}/change/", cls, end=end)


def _supply_events(ev, facts, today):
    for po in facts.get("purchases", []):
        end = po.received_date or po.expected_date
        if po.status == po.Status.RECEIVED:
            cls = "ok"
        elif po.status in (po.Status.DRAFT, po.Status.CANCELLED):
            cls = "plan"
        elif po.expected_date and po.expected_date < today:
            cls = "bad"
        else:
            cls = "open"
        who = po.supplier.name if po.supplier_id else ""
        ev("supply", "bar" if end else "point", po.order_date, f"🏭 {po}{f' · {who}' if who else ''}",
           f"🏭 {po} · {po.get_status_display()}{f' · {who}' if who else ''} · від {_fmt(po.order_date)}"
           + (f" · очікується {_fmt(po.expected_date)}" if po.expected_date else "")
           + (f" · отримано {_fmt(po.received_date)}" if po.received_date else ""),
           f"/admin/inventory/purchaseorder/{po.pk}/change/", cls, end=end)
    for j in facts.get("jlc", []):
        end = j.delivered_date or j.shipped_date or j.expected_date
        active = j.local_status not in (j.LocalStatus.DELIVERED, j.LocalStatus.CANCELLED)
        if j.local_status == j.LocalStatus.DELIVERED:
            cls = "ok"
        elif j.local_status == j.LocalStatus.CANCELLED:
            cls = "plan"
        elif active and j.expected_date and j.expected_date < today:
            cls = "bad"
        else:
            cls = "open"
        name = f"🔧 JLC {j.jlc_order_number or j.jlc_order_id}"
        ev("supply", "bar" if end else "point", j.order_date, f"{name} · {j.get_local_status_display()}",
           f"{name} · {j.get_order_type_display()} · {j.quantity} шт · {j.get_local_status_display()}"
           + (f" · {_cut(j.description, 60)}" if j.description else ""),
           f"/admin/jlcpcb/jlcorder/{j.pk}/change/", cls, end=end)


def _hours(project, viewer) -> list:
    """Години askDANTE по тижнях (зв'язок проєктів worktime.ProjectMap) — лише з розділом «Робочий час»."""
    try:
        from django.db.models import Sum
        from django.db.models.functions import TruncWeek
        from worktime.models import ProjectMap, TimeRecord
        from worktime.panels import can_see
        from worktime.services import _tree
    except ImportError:
        return []
    if not can_see(viewer):
        return []
    m = ProjectMap.objects.filter(project=project).select_related("dante_project").first()
    if m is None:
        return []
    rows = (TimeRecord.objects.filter(dante_project_id__in=_tree([m.dante_project]))
            .annotate(w=TruncWeek("day")).values("w").annotate(m=Sum("duration")).order_by("w"))
    shared = m.dante_project.maps.exclude(project=project).exists()
    return [{"lane": "hours", "shape": "hours", "start": _d(r["w"]), "end": _d(r["w"]) + timedelta(days=6),
             "minutes": r["m"], "label": "", "url": "", "cls": "",
             "tip": f"⏱ тиждень з {_fmt(r['w'])}: {_n(r['m'] / 60)} год"
                    + (" · спільний проєкт askDANTE" if shared else "")}
            for r in rows if r["w"] and r["m"]]


# ── Межі й малювання ─────────────────────────────────────────────────────────

def bounds(project, evs, today, months=None):
    if months:
        start = today - timedelta(days=int(months * 30.5))
    else:
        dates = [e["start"] for e in evs] + ([project.start_date] if project.start_date else [])
        start = min(dates) if dates else today - timedelta(days=30)
    ends = [today + timedelta(days=FUTURE_DAYS)]
    if project.due_date:
        ends.append(project.due_date + timedelta(days=7))
    limit = today + timedelta(days=MAX_FUTURE_DAYS)
    for e in evs:
        last = e["end"] or e["start"]
        if last <= limit:
            ends.append(last + timedelta(days=3))
    end = max(ends)
    start -= timedelta(days=3)
    if (end - start).days < MIN_DAYS:
        start = end - timedelta(days=MIN_DAYS)
    return start, end


class _Lane:
    """Розкладка подій доріжки по рядках: підпис праворуч від позначки, інакше ліворуч, інакше лише підказка."""

    def __init__(self):
        self.rows = []      # кінець зайнятого місця в кожному рядку

    def place(self, lo, hi):
        for i, end in enumerate(self.rows):
            if lo >= end + 4:
                self.rows[i] = hi
                return i
        if len(self.rows) < MAX_ROWS:
            self.rows.append(hi)
            return len(self.rows) - 1
        return None


def render(project, facts, viewer, months=None, today=None) -> str:
    """SVG шкали. months — останні N місяців (до строку / сьогодні + FUTURE_DAYS), None — усе."""
    today = today or timezone.localdate()
    evs = events(project, facts, viewer, today)
    start, end = bounds(project, evs, today, months)
    days = max((end - start).days, 1)
    ppd = (W - LEFT - RIGHT) / days

    def x(d):
        return LEFT + (d - start).days * ppd

    def xc(d):
        return x(d) + ppd / 2

    def clip(v):
        return min(max(v, LEFT), W - RIGHT)

    visible = [e for e in evs if (e["end"] or e["start"]) >= start and e["start"] <= end]
    by_lane = defaultdict(list)
    for e in visible:
        by_lane[e["lane"]].append(e)

    body, y = [], HEAD
    lane_index = 0
    for lane, title in LANES:
        items = sorted(by_lane.get(lane, []), key=lambda e: (e["start"], e.get("end") or e["start"]))
        if not items:
            continue
        top = y
        if lane == "mail":
            height, parts = _draw_mail(items, top, xc)
        elif lane == "hours":
            height, parts = _draw_hours(items, top, x)
        else:
            height, parts = _draw_lane(items, top, x, xc, clip)
        if lane_index % 2:
            body.append(f'<rect x="0" y="{_n(top)}" width="{W}" height="{_n(height)}" class="pjt-alt"/>')
        body.append(f'<text x="8" y="{_n(top + min(height, 30) / 2 + 4)}" class="pjt-lab">{escape(title)}</text>')
        body.extend(parts)
        y += height
        lane_index += 1

    if not body:
        return ('<div class="pjt-empty">На шкалі поки порожньо: додайте листування, Angebot, замовлення чи закупівлю '
                'до проєкту — вони з\'являться тут самі.</div>')

    total = y
    grid = _grid(start, end, x, ppd, total)
    marks, labels = [], []
    for d, cls, label in ([(project.start_date, "pjt-start", f"старт {project.start_date:%d.%m}")]
                          if project.start_date else []) + \
                         ([(project.due_date, "pjt-due", f"строк клієнту {project.due_date:%d.%m}")]
                          if project.due_date else []) + [(today, "pjt-today", "сьогодні")]:
        if start <= d <= end:
            xx = xc(d)
            marks.append(f'<line x1="{_n(xx)}" y1="{HEAD - 6}" x2="{_n(xx)}" y2="{_n(total)}" class="{cls}"/>')
            labels.append((xx, cls, label))
    foot = []
    used = []
    for xx, cls, label in sorted(labels):
        w = len(label) * CHAR_W
        lo = min(max(xx - w / 2, LEFT), W - RIGHT - w)
        row = 0 if not used or lo > used[-1] + 6 else 1
        if row == 0:
            used.append(lo + w)
        foot.append(f'<text x="{_n(lo)}" y="{_n(total + 14 + row * 13)}" class="pjt-ft {cls}-t">{escape(label)}</text>')
    height = total + 32
    summary = (f"Шкала проєкту {project.code}: {len(visible)} подій з {start:%d.%m.%Y} по {end:%d.%m.%Y}"
               + (f", строк клієнту {project.due_date:%d.%m.%Y}" if project.due_date else ""))
    return (f'<svg class="pjt" viewBox="0 0 {W} {_n(height)}" role="img" aria-label="{escape(summary)}" '
            f'data-start="{start.isoformat()}" data-end="{end.isoformat()}">'
            + "".join(grid) + "".join(body) + "".join(marks) + "".join(foot) + "</svg>")


def _grid(start, end, x, ppd, total) -> list:
    out = [f'<line x1="{LEFT - 6}" y1="{HEAD - 4}" x2="{LEFT - 6}" y2="{_n(total)}" class="pjt-grid"/>']
    months = ["січ", "лют", "бер", "кві", "тра", "чер", "лип", "сер", "вер", "жов", "лис", "гру"]
    if ppd * 7 >= 14:                              # тижні — понеділки
        d = start + timedelta(days=(7 - start.weekday()) % 7)
        while d <= end:
            out.append(f'<line x1="{_n(x(d))}" y1="{HEAD - 4}" x2="{_n(x(d))}" y2="{_n(total)}" class="pjt-wk"/>')
            if ppd * 7 >= 34:
                out.append(f'<text x="{_n(x(d) + 2)}" y="{HEAD - 7}" class="pjt-wkt">{d:%d.%m}</text>')
            d += timedelta(days=7)
    d = start.replace(day=1)
    while d <= end:
        if d >= start:
            out.append(f'<line x1="{_n(x(d))}" y1="2" x2="{_n(x(d))}" y2="{_n(total)}" class="pjt-mon"/>')
        label_x = max(x(d), LEFT) + 4
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        if x(nxt) - label_x > 30:
            name = months[d.month - 1] + (f" {d.year}" if d.month == 1 or d <= start else "")
            out.append(f'<text x="{_n(label_x)}" y="12" class="pjt-mont">{name}</text>')
        d = nxt
    return out


def _attrs(e) -> str:
    return f'<title>{escape(e["tip"] or e["label"])}</title>'


def _wrap(e, inner) -> str:
    data = f' data-hid="{e["hid"]}"' if e.get("hid") else ""
    g = f'<g class="pjt-ev pjt-{e["cls"] or "def"}"{data}>{_attrs(e)}{inner}</g>'
    if e["url"]:
        return f'<a href="{escape(e["url"])}" target="_blank" rel="noopener">{g}</a>'
    return g


def _draw_lane(items, top, x, xc, clip):
    lane = _Lane()
    placed = []
    for e in items:
        point = e["shape"] != "bar"
        x1 = clip(xc(e["start"])) if point else clip(x(e["start"]))
        x2 = x1 if point else max(clip(x(e["end"] + timedelta(days=1))), x1 + 3)
        label = e["label"]
        lw = len(label) * CHAR_W
        anchor, lx = "start", x2 + (9 if point else 5)
        lo, hi = x1 - 7, lx + lw
        if label and hi > W - RIGHT:                   # не влазить праворуч — ліворуч від позначки
            anchor, lx = "end", x1 - (9 if point else 5)
            lo, hi = lx - lw, x2 + 7
            if lo < LEFT:
                label, lo, hi = "", x1 - 7, x2 + 7     # лише позначка, текст — у підказці
        if not label:
            lo, hi = x1 - 7, x2 + 7
        row = lane.place(lo, hi)
        if row is None:                                # місця немає — позначка в першому рядку без підпису
            row, label = 0, ""
        placed.append((e, row, x1, x2, label, anchor, lx))
    rows = max(len(lane.rows), 1)
    height = LANE_PAD * 2 + rows * ROW_H
    parts = []
    for e, row, x1, x2, label, anchor, lx in placed:
        cy = top + LANE_PAD + row * ROW_H + ROW_H / 2
        shape = e["shape"]
        if shape == "bar":
            inner = f'<rect x="{_n(x1)}" y="{_n(cy - 5)}" width="{_n(x2 - x1)}" height="10" rx="3" class="pjt-bar"/>'
        elif shape == "diamond":
            inner = (f'<polygon points="{_n(x1)},{_n(cy - 6)} {_n(x1 + 6)},{_n(cy)} {_n(x1)},{_n(cy + 6)} '
                     f'{_n(x1 - 6)},{_n(cy)}" class="pjt-dia"/>')
        elif shape == "flag":
            inner = (f'<line x1="{_n(x1)}" y1="{_n(cy - 7)}" x2="{_n(x1)}" y2="{_n(cy + 7)}" class="pjt-pole"/>'
                     f'<polygon points="{_n(x1)},{_n(cy - 7)} {_n(x1 + 8)},{_n(cy - 4)} {_n(x1)},{_n(cy - 1)}" '
                     f'class="pjt-flag"/>')
        else:
            inner = f'<circle cx="{_n(x1)}" cy="{_n(cy)}" r="4.5" class="pjt-pt"/>'
        if label:
            end_attr = ' text-anchor="end"' if anchor == "end" else ""
            inner += f'<text x="{_n(lx)}" y="{_n(cy + 4)}" class="pjt-t"{end_attr}>{escape(label)}</text>'
        parts.append(_wrap(e, inner))
    return height, parts


def _draw_mail(items, top, xc):
    parts = []
    for e in items:
        cy = top + (12 if e["cls"] == "in" else 25)
        r = min(3 + (e["count"] - 1) * 0.8, 6.5)
        inner = f'<circle cx="{_n(xc(e["start"]))}" cy="{_n(cy)}" r="{_n(r)}" class="pjt-dot"/>'
        if e.get("ring"):
            inner += f'<circle cx="{_n(xc(e["start"]))}" cy="{_n(cy)}" r="{_n(r + 3)}" class="pjt-ring"/>'
        data = ""
        if e.get("mails"):
            data = f' data-mails="{escape(json.dumps(e["mails"], ensure_ascii=False))}"'
        g = f'<g class="pjt-ev pjt-{e["cls"]}"{data}>{_attrs(e)}{inner}</g>'
        parts.append(f'<a href="{escape(e["url"])}" target="_blank" rel="noopener">{g}</a>' if e["url"] else g)
    return MAIL_H, parts


def _draw_hours(items, top, x):
    top_minutes = max(e["minutes"] for e in items) or 1
    base = top + HOURS_H - 6
    parts = []
    for e in items:
        x1, x2 = x(e["start"]) + 1.5, x(e["end"] + timedelta(days=1)) - 1.5
        h = max(e["minutes"] / top_minutes * 24, 1.5)
        inner = f'<rect x="{_n(x1)}" y="{_n(base - h)}" width="{_n(max(x2 - x1, 1.5))}" height="{_n(h)}" rx="2" class="pjt-hrs"/>'
        if x2 - x1 >= 18:
            inner += (f'<text x="{_n((x1 + x2) / 2)}" y="{_n(base - h - 3)}" text-anchor="middle" class="pjt-ht">'
                      f'{_n(e["minutes"] / 60).replace(".", ",")}</text>')
        parts.append(f'<g class="pjt-ev">{_attrs(e)}{inner}</g>')
    total = sum(e["minutes"] for e in items)
    parts.append(f'<text x="{W - RIGHT}" y="{_n(top + 12)}" text-anchor="end" class="pjt-ht">'
                 f'Σ {_n(total / 60).replace(".", ",")} год</text>')
    return HOURS_H, parts
