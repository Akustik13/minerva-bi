"""📌 Панель праворуч для списку замовлень (core/side_panel.py): що потрібно бачити поруч зі списком —
позиції й склад, доставка, документи, листування, нотатки — і швидкі дії. Лише читання."""
from urllib.parse import quote, urlencode

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from core.side_panel import Action, Block, register

STATUS_TONE = {"received": "new", "processing": "work", "shipped": "ship", "delivered": "done", "cancelled": "off"}


def _num(v):
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",")
    except (TypeError, ValueError):
        return ""


def _qty(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f == int(f) else str(f).replace(".", ",")


def _rows(rows):
    return format_html_join("", '<div class="mvp-row"><span>{}</span><span class="{}">{}</span></div>', rows)


def title(o):
    tone = STATUS_TONE.get(o.status, "new")
    who = " · ".join(x for x in (o.client or o.contact_name, o.source, (_num(o.total_price) + " " + (o.currency or ""))
                                 if o.total_price is not None else "") if x)
    return format_html('<b>№ {}</b> <span class="mvp-badge mvp-t-{}">{}</span><div class="mvp-sub">{}</div>',
                       o.order_number, tone, o.get_status_display(), who)


def summary(o, request):
    rows = []
    if o.contact_name and o.contact_name != o.client:
        rows.append(("Контакт", "", o.contact_name))
    if o.email:
        rows.append(("Email", "", o.email))
    if o.phone:
        rows.append(("Телефон", "", o.phone))
    place = ", ".join(x for x in (o.addr_city, o.addr_country) if x)
    if place:
        rows.append(("Країна, місто", "", place))
    if o.order_date:
        rows.append(("Дата замовлення", "", o.order_date.strftime("%d.%m.%Y")))
    if o.payment_status:
        rows.append(("Оплата", "", o.get_payment_status_display() if hasattr(o, "get_payment_status_display")
                     else o.payment_status))
    crm = ""
    if o.customer_key:
        from crm.models import Customer
        c = Customer.objects.filter(external_key=o.customer_key).only("pk", "name", "company").first()
        if c:
            crm = format_html('<a class="mvp-link" href="{}">👤 {} — картка CRM</a>',
                              reverse("admin:crm_customer_change", args=[c.pk]), c.company or c.name or "клієнт")
    return format_html("{}{}", _rows(rows), crm)


def lines(o, request):
    from inventory.services.stock import order_lines_cover
    items = list(o.lines.select_related("product").order_by("pk"))
    if not items:
        return format_html('<div class="mvp-empty">Позицій немає.</div>')
    try:
        cover = order_lines_cover(o)
    except Exception:  # noqa: BLE001 — склад недоступний: позиції без нього
        cover = {}
    out = []
    for line in items:
        sku = line.sku_raw or (line.product.sku if line.product_id else "")
        c = cover.get(line.pk)
        if c is None or o.status in ("shipped", "delivered", "cancelled"):
            state, tone, detail = "", "", ""
        else:
            free = c["free"]
            short = line.qty - free
            if short > 0:
                state, tone = f"бракує {_qty(short)}", "err"
            else:
                state, tone = "вистачає", "ok"
            detail = f"вільно {_qty(c['available'])}" + (f" · це замовлення тримає {_qty(c['held'])}" if c["held"] else "")
        link = reverse("admin:inventory_product_change", args=[line.product_id]) if line.product_id else ""
        out.append(format_html(
            '<div class="mvp-line"><div class="mvp-row"><span><a class="mvp-link" href="{}">{}</a> × {}</span>'
            '<span class="mvp-{}">{}</span></div><div class="mvp-note">{}</div></div>',
            link or "#", sku, _qty(line.qty), tone, state, detail))
    return format_html("{}", format_html_join("", "{}", ((x,) for x in out)))


def shipping(o, request):
    rows = []
    if o.shipping_deadline:
        late = o.shipping_deadline < timezone.localdate() and o.status in ("received", "processing")
        rows.append(("Відправити до", "mvp-err" if late else "", o.shipping_deadline.strftime("%d.%m.%Y")
                     + (" · строк минув" if late else "")))
    ships = list(o.shipments.select_related("carrier").order_by("-pk")[:3]) if hasattr(o, "shipments") else []
    for s in ships:
        carrier = getattr(s.carrier, "name", "") or "Відправлення"
        rows.append((carrier, "", f"{s.get_status_display()}" + (f" · {s.tracking_number}" if s.tracking_number else "")))
    if not ships and (o.shipping_courier or o.tracking_number):
        rows.append((o.shipping_courier or "Кур'єр", "", o.tracking_number or "—"))
    if o.shipped_at:
        rows.append(("Відправлено", "", o.shipped_at.strftime("%d.%m.%Y")))
    addr = ", ".join(x for x in (o.ship_company or o.client, o.addr_street, " ".join(
        x for x in (o.addr_zip, o.addr_city) if x), o.addr_country) if x)
    tail = format_html('<div class="mvp-note">{}</div>', addr) if addr else ""
    if not rows and not addr:
        return format_html('<div class="mvp-empty">Доставки ще немає.</div>')
    return format_html("{}{}", _rows(rows), tail)


def documents(o, request):
    from pathlib import Path
    folder = Path(settings.MEDIA_ROOT) / "orders" / (o.source or "manual") / o.order_number
    try:
        files = sorted(p for p in folder.iterdir() if p.is_file()) if folder.exists() else []
    except OSError:
        files = []
    if not files:
        return format_html('<div class="mvp-empty">Документів немає.</div>')
    base = f"{settings.MEDIA_URL}orders/{quote(o.source or 'manual')}/{quote(o.order_number)}/"
    return format_html('<div class="mvp-chips">{}</div>', format_html_join(
        "", '<a class="mvp-chip" href="{}" target="_blank" rel="noopener">📄 {}</a>',
        ((base + quote(f.name), f.name) for f in files[:12])))


def mail(o, request):
    """Листи про це замовлення — лише з власних скриньок людини (як усюди в пошті Minerva)."""
    from crm.models import CustomerTimeline
    events = (CustomerTimeline.objects.filter(related_order_id=o.pk, email_message__isnull=False,
                                              email_message__account__user=request.user)
              .select_related("email_message").order_by("-email_message__sent_at", "-pk")[:5])
    rows = []
    for e in events:
        m = e.email_message
        when = timezone.localtime(m.sent_at).strftime("%d.%m %H:%M") if m.sent_at else ""
        rows.append((m.from_name or m.from_email, when, m.subject or "(без теми)"))
    search = reverse("email_assistant:inbox") + "?" + urlencode({"q": o.order_number}) if _has_inbox() else ""
    more = format_html('<a class="mvp-link" href="{}">🔍 Усі листи з № {}</a>', search, o.order_number) if search else ""
    if not rows:
        return format_html('<div class="mvp-empty">Листів про це замовлення у ваших скриньках немає.</div>{}', more)
    return format_html("{}{}", format_html_join(
        "", '<div class="mvp-mail"><div class="mvp-row"><b>{}</b><span class="mvp-note">{}</span></div>'
            '<div class="mvp-note">{}</div></div>', rows), more)


def _has_inbox():
    try:
        reverse("email_assistant:inbox")
        return True
    except Exception:  # noqa: BLE001
        return False


def notes(o, request):
    from crm.models import CustomerTimeline
    events = (CustomerTimeline.objects.filter(related_order_id=o.pk, email_message__isnull=True)
              .order_by("-created_at")[:4])
    out = []
    if o.internal_note:
        out.append(format_html('<div class="mvp-quote">📝 {}</div>', o.internal_note))
    out += [format_html('<div class="mvp-row"><span>{}</span><span class="mvp-note">{}</span></div>', e.title,
                        timezone.localtime(e.created_at).strftime("%d.%m")) for e in events]
    if not out:
        return format_html('<div class="mvp-empty">Нотаток немає.</div>')
    return format_html("{}", format_html_join("", "{}", ((x,) for x in out)))


def _compose(o):
    if not o.email or not _has_inbox():
        return ""
    return reverse("email_assistant:compose") + "?" + urlencode({"to": o.email, "subject": f"№ {o.order_number}"})


def _crm(o):
    if not o.customer_key:
        return ""
    from crm.models import Customer
    c = Customer.objects.filter(external_key=o.customer_key).only("pk").first()
    return reverse("admin:crm_customer_change", args=[c.pk]) if c else ""


register(
    "sales.salesorder", title=title,
    blocks=[
        Block("summary", "Клієнт і замовлення", summary),
        Block("lines", "Позиції й склад", lines),
        Block("shipping", "Доставка", shipping),
        Block("documents", "Документи", documents),
        Block("mail", "Листування", mail),
        Block("notes", "Нотатки й події", notes, default=False),
    ],
    actions=[
        Action("open", "📋 Відкрити картку", lambda o: reverse("admin:sales_salesorder_change", args=[o.pk])),
        Action("label", "🏷 Етикетка", lambda o: reverse("labels:order", args=[o.pk])),
        Action("mail", "✉️ Лист клієнту", _compose),
        Action("shipments", "🚚 Відправлення",
               lambda o: reverse("admin:shipping_shipment_changelist") + f"?order__id__exact={o.pk}"),
        Action("crm", "👤 Клієнт CRM", _crm, default=False),
    ],
)
