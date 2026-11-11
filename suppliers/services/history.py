"""
suppliers/services/history.py — 🧵 історія постачальника з пошти: листування за темами (запит → пропозиція → PI /
рахунок), справжні файли (Quotation, PI, Gerber, BOM…) без картинок з підписів, події «🕘 Історії» на датах листів,
закупівля з теми з PI. Лише листи ВЛАСНИХ скриньок людини (mailscan.messages).

  topic_of(subject)   — тема без «Re: / AW: / Fw: / Re[4]:»;
  is_document(att)    — вкладення-документ (не вбудована картинка, не логотип / image001.png з підпису);
  doc_kind(name)      — quotation / pi / invoice / confirmation / packing / gerber / bom / pnp / stackup / drawing / other;
  rebuild(s, user)    — пошта за YEARS років → SupplierAnalysis MAIL (теми) + події історії (без повторів);
  make_purchase(...)  — закупівля з теми: постачальник, дата й номер з PI, документи теми → документи закупівлі.
"""
import datetime
import re

from django.utils import timezone

YEARS = 5
TOPICS_MAX = 60
DOCS_PER_TOPIC = 40
_PREFIX_RE = re.compile(r"^\s*((re|aw|antw|antwort|fw|fwd|wg|tr|sv|vs|rv|r|ответ|відп)\s*(\[\d+\]|\(\d+\))?\s*[:：]\s*)+",
                        re.I)
_GENERIC_IMG_RE = re.compile(r"^(image|img|pic|photo|outlook|~wrl|logo|signature|sig|banner|icon|unnamed|"
                             r"[0-9a-f]{8}-[0-9a-f]{4}-|[a-z0-9]{6,10}\.(png|gif|jpe?g)$)", re.I)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
IMAGE_MIN = 150 * 1024
DOC_KINDS = [
    ("pi", "💳 PI / проформа", re.compile(r"(^|[^a-z])(pi|proforma|pro-forma)([^a-z]|$)", re.I)),
    ("invoice", "🧾 Рахунок", re.compile(r"invoice|rechnung|发票|факту|рахун", re.I)),
    ("confirmation", "✅ Підтвердження", re.compile(r"order\s*confirm|auftragsbest|(^|[^a-z])oc([^a-z]|$)", re.I)),
    ("quotation", "💶 Пропозиція", re.compile(r"quot|quote|angebot|offer|报价|пропозиц|\bkp\b", re.I)),
    ("packing", "📦 Пакування / доставка", re.compile(r"packing|lieferschein|delivery\s*note|awb|waybill", re.I)),
    ("bom", "📋 BOM", re.compile(r"(^|[^a-z])bom([^a-z]|$)|stückliste|bill.?of.?mat", re.I)),
    ("pnp", "📍 Pick & Place", re.compile(r"pandp|pick.?and.?place|centroid|(^|[^a-z])pnp([^a-z]|$)", re.I)),
    ("stackup", "🧱 Стекап", re.compile(r"stack.?up|lagenaufbau", re.I)),
    ("gerber", "🗜 Gerber / архів", re.compile(r"gerber|\.(zip|rar|7z)$", re.I)),
    ("drawing", "📐 Креслення", re.compile(r"\.(dxf|step|stp|dwg|igs)$|drawing|zeichnung|кресл", re.I)),
]
KIND_NAMES = {k: n for k, n, _rx in DOC_KINDS} | {"other": "📎 Файл"}
STAGES = {"pi": "💳 PI / рахунок", "confirmation": "✅ Підтверджено", "quotation": "💶 Пропозиція",
          "request": "📨 Наш запит", "marketing": "📣 Розсилка від них", "mail": "✉️ Листування"}
MARKETING_RE = re.compile(r"peak season|new year|holiday|christmas|promotion|special offer|any (pcba?|pcb) needs|"
                          r"follow.?up the|how to support|ensuring your|newsletter|catalog(ue)?\b", re.I)


def topic_of(subject) -> str:
    t = _PREFIX_RE.sub("", str(subject or "")).strip()
    for _ in range(3):                                     # «Re: Fw: Re[4]:» — кілька разів
        t2 = _PREFIX_RE.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    return " ".join(t.split())[:200] or "(без теми)"


def is_document(att) -> bool:
    if not isinstance(att, dict) or att.get("is_inline"):
        return False
    name = (att.get("name") or "").strip()
    low = name.lower()
    if not name:
        return False
    if low.endswith(IMAGE_EXT):          # картинка — лише велика й не «image001.png» з підпису
        return (att.get("size") or 0) >= IMAGE_MIN and not _GENERIC_IMG_RE.match(low)
    if low.endswith((".ics", ".vcf", ".p7s", ".asc", ".sig")):
        return False
    return True


def doc_kind(name) -> str:
    for key, _label, rx in DOC_KINDS:
        if rx.search(name or ""):
            return key
    return "other"


def stage_of(group) -> str:
    kinds = {d["kind"] for d in group["documents"] if d.get("theirs")}
    msgs = group["messages"]
    if kinds & {"pi", "invoice"}:
        return "pi"
    if "confirmation" in kinds:
        return "confirmation"
    if "quotation" in kinds:
        return "quotation"
    ours = [m for m in msgs if getattr(m, "ours", False)]
    if not ours and msgs and MARKETING_RE.search(group.get("title") or ""):
        return "marketing"
    if msgs and getattr(msgs[0], "ours", False):
        return "request"
    return "mail"


def _serialize(group) -> dict:
    msgs = group["messages"]
    first, last = msgs[0], msgs[-1]
    stage = stage_of(group)
    return {
        "key": group["key"], "title": group.get("title") or "", "stage": stage,
        "po": group["po"].pk if group.get("po") else None, "po_code": group["po"].code if group.get("po") else "",
        "first": first.sent_at.isoformat() if first.sent_at else "", "last": last.sent_at.isoformat() if last.sent_at else "",
        "incoming": sum(1 for m in msgs if getattr(m, "theirs", False)),
        "outgoing": sum(1 for m in msgs if getattr(m, "ours", False)),
        "first_msg": first.pk, "last_msg": last.pk,
        "waiting_us": bool(getattr(last, "theirs", False)),
        "docs": [{"msg": d["message"].pk, "i": d["index"], "name": d["name"][:120], "kind": d["kind"],
                  "theirs": d.get("theirs", False),
                  "date": d["message"].sent_at.date().isoformat() if d["message"].sent_at else ""}
                 for d in group["documents"][:DOCS_PER_TOPIC]],
    }


def rebuild(supplier, user, years=YEARS) -> dict:
    """Пошта людини за `years` років → теми в SupplierAnalysis MAIL + події історії. → {topics, events, messages}"""
    from suppliers.services import mailscan
    today = timezone.localdate()
    result = mailscan.scan(supplier, user, today - datetime.timedelta(days=365 * years), today)
    return save(supplier, user, result)


def save(supplier, user, result) -> dict:
    """Результат mailscan.scan → SupplierAnalysis MAIL людини (числа + теми) і події історії."""
    from suppliers.models import SupplierAnalysis
    from suppliers.services import mailscan
    topics = [_serialize(g) for g in result["groups"]][:TOPICS_MAX]
    SupplierAnalysis.objects.update_or_create(
        supplier=supplier, kind=SupplierAnalysis.MAIL, owner=user,
        defaults={"date_from": result["date_from"], "date_to": result["date_to"],
                  "result": {**mailscan.summary(result), "topics": topics}, "created_at": timezone.now()})
    events = _events(supplier, user, result)
    return {"topics": len(topics), "events": events, "messages": result["messages"]}


def _events(supplier, user, result) -> int:
    """Події «🕘 Історії» на датах листів: перше листування, початок кожної теми, документи-віхи (пропозиція,
    PI, рахунок, підтвердження). Те саме (лист + текст) удруге не пишеться."""
    from suppliers.models import SupplierEvent
    have = set(SupplierEvent.objects.filter(supplier=supplier, message__isnull=False)
               .values_list("message_id", "text"))
    new = []

    def add(msg, kind, text):
        if (msg.pk, text) in have:
            return
        have.add((msg.pk, text))
        new.append(SupplierEvent(supplier=supplier, kind=kind, message=msg, user=user, text=text[:500],
                                 created_at=msg.sent_at or timezone.now()))

    groups = sorted(result["groups"], key=lambda g: g["messages"][0].sent_at or timezone.now())
    if groups:
        first = min((g["messages"][0] for g in groups), key=lambda m: m.sent_at or timezone.now())
        add(first, SupplierEvent.MAIL, f"✉️ Перше листування: «{topic_of(first.subject)}»")
    for g in groups:
        m0 = g["messages"][0]
        stage = stage_of(g)
        if stage == "marketing":
            continue
        who = "📨 Наш запит" if getattr(m0, "ours", False) else "✉️ Від них"
        add(m0, SupplierEvent.MAIL, f"{who}: «{g.get('title') or topic_of(m0.subject)}» "
                                    f"(листів {len(g['messages'])}, файлів {len(g['documents'])})")
        for d in g["documents"]:
            if d["kind"] in ("quotation", "pi", "invoice", "confirmation") and d.get("theirs"):
                add(d["message"], SupplierEvent.MAIL,
                    f"{KIND_NAMES[d['kind']]}: {d['name']} — «{g.get('title') or ''}»")
    if new:
        SupplierEvent.objects.bulk_create(new)
    return len(new)


def topics_for(supplier, user) -> dict | None:
    from suppliers.models import SupplierAnalysis
    a = SupplierAnalysis.objects.filter(supplier=supplier, kind=SupplierAnalysis.MAIL, owner=user).first()
    if a is None or "topics" not in (a.result or {}):
        return None
    return {"topics": a.result["topics"], "at": a.created_at, "date_from": a.date_from, "date_to": a.date_to}


def make_purchase(supplier, user, topic_key):
    """🛒 Закупівля з теми: PI / рахунок → дата й номер замовлення постачальника, документи теми (PI, пропозиція,
    Gerber, BOM…) → документи закупівлі. Тема вже з закупівлею — ValueError."""
    from inventory.models import PurchaseOrder, PurchaseOrderDocument as D
    from inventory.services import po_documents
    from suppliers.models import SupplierEvent
    t = next((x for x in (topics_for(supplier, user) or {}).get("topics", []) if x["key"] == topic_key), None)
    if t is None:
        raise ValueError("Тему не знайдено — оновіть історію з пошти.")
    if t.get("po"):
        raise ValueError(f"Тема вже прив'язана до закупівлі {t.get('po_code')}.")
    bill = next((d for d in t["docs"] if d["kind"] in ("pi", "invoice") and d.get("theirs")), None) or \
        next((d for d in t["docs"] if d["kind"] == "quotation" and d.get("theirs")), None)
    number = ""
    if bill:
        stem = re.sub(r"\.[a-z0-9]{2,4}$", "", bill["name"], flags=re.I)   # «PI#20260402MC02.pdf» → 20260402MC02
        tokens = [x for x in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{5,}", stem) if re.search(r"\d", x)]
        number = max(tokens, key=len)[:64] if tokens else ""
    date = datetime.date.fromisoformat(bill["date"]) if bill and bill.get("date") else timezone.localdate()
    po = PurchaseOrder.objects.create(
        supplier=supplier, status=PurchaseOrder.Status.ORDERED, order_date=date, supplier_order_no=number,
        currency=supplier.currency or "EUR", notes=f"З листування: «{t['title']}»"[:2000])
    cat = {"pi": D.INVOICE, "invoice": D.INVOICE, "confirmation": D.CONFIRMATION, "quotation": D.QUOTE,
           "packing": D.SHIPPING, "gerber": D.RFQ, "bom": D.RFQ, "pnp": D.RFQ, "stackup": D.RFQ, "drawing": D.RFQ}
    picks, seen = {}, set()
    for d in t["docs"]:
        if (d["name"], d["kind"]) in seen:
            continue                                     # той самий файл у кількох листах гілки — один раз
        seen.add((d["name"], d["kind"]))
        picks[f"{d['msg']}:{d['i']}"] = cat.get(d["kind"], D.OTHER)
    out = po_documents.add_from_mail(po, user, picks) if picks else {"added": 0, "same": 0, "failed": []}
    SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.NOTE, user=user,
                                 text=f"🛒 Закупівля {po.code} з листування «{t['title'][:120]}» "
                                      f"(документів: {out['added']})")
    return po, out
