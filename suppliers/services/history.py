"""
suppliers/services/history.py — 🧵 історія постачальника з пошти: листування за темами (запит → пропозиція → PI /
рахунок), справжні файли (Quotation, PI, Gerber, BOM…) без картинок з підписів, події «🕘 Історії» на датах листів,
закупівля з теми з PI. Лише листи ВЛАСНИХ скриньок людини (mailscan.messages).

  topic_of(subject)   — тема без «Re: / AW: / Fw: / Re[4]:»;
  is_document(att)    — вкладення-документ (не вбудована картинка, не логотип / image001.png з підпису);
  doc_kind(name)      — quotation / pi / invoice / confirmation / packing / gerber / bom / pnp / stackup / drawing / other;
  rebuild(s, user)    — пошта за YEARS років → SupplierAnalysis MAIL (теми) + події історії (без повторів);
  make_purchase(...)  — закупівля з теми: постачальник, дата й номер з PI, документи теми → документи закупівлі;
                        as_history — уже отримана (Received без позицій: без складу й без «Очікується»);
  make_purchases(...) — те саме для всіх тем з PI / підтвердженням, яких ще немає серед закупівель.
"""
import datetime
import re

from django.utils import timezone

YEARS = 5
FRESH_DAYS = 30            # тема з листом за останні N днів — закупівля ще може бути в дорозі
HISTORY_NOTE = ("📜 Історія з листування: заведено вже отриманою — без складу й без «Очікується», "
                "дата отримання невідома. Позицій не додавайте — інакше вони потраплять на склад.")
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
        "po_history": bool(group.get("po") and is_history(group["po"])),
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


_MILESTONE_DOCS = ("quotation", "pi", "invoice", "confirmation")


def _event_key(msg_id, text):
    """Ключ віхи без назви теми й лічильників: тема стала закупівлею («RFQ…» → «PO-4») чи в ній додались листи —
    це та сама віха, удруге не пишеться. Інші події листа — за повним текстом."""
    if text.startswith("✉️ Перше листування"):
        return (msg_id, "first")
    if text.startswith(("📨 Наш запит: «", "✉️ Від них: «")):
        return (msg_id, "start")
    head = text.split(" — «", 1)[0]
    if head != text and head.split(":", 1)[0] in {KIND_NAMES[k] for k in _MILESTONE_DOCS}:
        return (msg_id, "doc", head)
    return (msg_id, text)


def _events(supplier, user, result) -> int:
    """Події «🕘 Історії» на датах листів: перше листування, початок кожної теми, документи-віхи (пропозиція,
    PI, рахунок, підтвердження). Та сама віха (_event_key) удруге не пишеться; повтори, що лишились з версії,
    яка порівнювала повний текст (тема → закупівля давала «…— «PO-4»» поруч із «…— «RFQ…»»), прибираються."""
    from suppliers.models import SupplierEvent
    have, dupes = set(), []
    for pk, msg_id, text in (SupplierEvent.objects.filter(supplier=supplier, message__isnull=False)
                             .order_by("pk").values_list("pk", "message_id", "text")):
        key = _event_key(msg_id, text)
        if key in have and len(key) > 1 and key[1] in ("first", "start", "doc"):
            dupes.append(pk)
        have.add(key)
    if dupes:
        SupplierEvent.objects.filter(pk__in=dupes).delete()
    new = []

    def add(msg, kind, text):
        key = _event_key(msg.pk, text)
        if key in have:
            return
        have.add(key)
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
            if d["kind"] in _MILESTONE_DOCS and d.get("theirs"):
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


def is_history(po) -> bool:
    """Закупівля заведена з листування як історія (make_purchase(as_history=True))."""
    return HISTORY_NOTE[:40] in (po.notes or "")


def can_purchase(t) -> bool:
    """Тема, з якої можна завести закупівлю: PI / рахунок або підтвердження від них, закупівлі ще немає."""
    return not t.get("po") and t.get("stage") in ("pi", "confirmation")


def history_default(t, today=None) -> bool:
    """Галочка «📜 як історію» за замовчуванням: останній лист теми старший за FRESH_DAYS днів."""
    try:
        last = datetime.date.fromisoformat((t.get("last") or "")[:10])
    except ValueError:
        return True
    return ((today or timezone.localdate()) - last).days > FRESH_DAYS


def make_purchase(supplier, user, topic_key, as_history=False):
    """🛒 Закупівля з теми: PI / рахунок → дата й номер замовлення постачальника, документи теми (PI, пропозиція,
    Gerber, BOM…) → документи закупівлі. Тема вже з закупівлею — ValueError.

    as_history — 📜 закупівля, яка давно прийшла: стан «Отримано» без позицій, тож склад і «Очікується» не
    змінюються, а дата отримання лишається порожньою (з пошти її не видно — оцінка строків постачальника її не
    враховує). Тему й рядок закупівлі блокуємо разом (select_for_update аналізу людини): подвійне натискання не
    заведе двох закупівель; документи — після, бо можуть читатися з IMAP."""
    from django.db import transaction

    from inventory.models import PurchaseOrder, PurchaseOrderDocument as D
    from inventory.services import po_documents
    from suppliers.models import SupplierAnalysis, SupplierEvent
    with transaction.atomic():
        a = (SupplierAnalysis.objects.select_for_update()
             .filter(supplier=supplier, kind=SupplierAnalysis.MAIL, owner=user).first())
        topics = (a.result or {}).get("topics", []) if a is not None else []
        t = next((x for x in topics if x["key"] == topic_key), None)
        if t is None:
            raise ValueError("Тему не знайдено — оновіть історію з пошти.")
        if t.get("po"):
            raise ValueError(f"Тема вже прив'язана до закупівлі {t.get('po_code')}.")
        bill = next((d for d in t["docs"] if d["kind"] in ("pi", "invoice") and d.get("theirs")), None) or \
            next((d for d in t["docs"] if d["kind"] == "quotation" and d.get("theirs")), None)
        number = ""
        if bill:
            stem = re.sub(r"\.[a-z0-9]{2,4}$", "", bill["name"], flags=re.I)  # «PI#20260402MC02.pdf» → 20260402MC02
            tokens = [x for x in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{5,}", stem) if re.search(r"\d", x)]
            number = max(tokens, key=len)[:64] if tokens else ""
        date = datetime.date.fromisoformat(bill["date"]) if bill and bill.get("date") else timezone.localdate()
        notes = f"З листування: «{t['title']}»"[:1700] + (f"\n{HISTORY_NOTE}" if as_history else "")
        po = PurchaseOrder.objects.create(
            supplier=supplier, status=PurchaseOrder.Status.RECEIVED if as_history else PurchaseOrder.Status.ORDERED,
            order_date=date, supplier_order_no=number, currency=supplier.currency or "EUR", notes=notes)
        t.update(po=po.pk, po_code=po.code, po_history=as_history)          # тема одразу під закупівлею
        SupplierAnalysis.objects.filter(pk=a.pk).update(result=a.result)
    cat = {"pi": D.INVOICE, "invoice": D.INVOICE, "confirmation": D.CONFIRMATION, "quotation": D.QUOTE,
           "packing": D.SHIPPING, "gerber": D.RFQ, "bom": D.RFQ, "pnp": D.RFQ, "stackup": D.RFQ, "drawing": D.RFQ}
    picks, seen = {}, set()
    for d in t["docs"]:
        if (d["name"], d["kind"]) in seen:
            continue                                     # той самий файл у кількох листах гілки — один раз
        seen.add((d["name"], d["kind"]))
        picks[f"{d['msg']}:{d['i']}"] = cat.get(d["kind"], D.OTHER)
    out = po_documents.add_from_mail(po, user, picks) if picks else {"added": 0, "same": 0, "failed": []}
    if as_history:
        text = (f"📜 Закупівля {po.code} з листування «{t['title'][:120]}» — історія, без складу "
                f"(документів: {out['added']})")
    else:
        text = f"🛒 Закупівля {po.code} з листування «{t['title'][:120]}» (документів: {out['added']})"
    SupplierEvent.objects.create(supplier=supplier, kind=SupplierEvent.NOTE, user=user, text=text)
    return po, out


def make_purchases(supplier, user, as_history=True) -> dict:
    """Закупівлі з усіх тем з PI / підтвердженням, яких ще немає (can_purchase). → {pos, docs, failed}"""
    pos, docs, failed = [], 0, []
    for t in list((topics_for(supplier, user) or {}).get("topics", [])):
        if not can_purchase(t):
            continue
        try:
            po, out = make_purchase(supplier, user, t["key"], as_history=as_history)
        except ValueError as e:                     # тему щойно завели в іншому вікні
            failed.append(str(e))
            continue
        pos.append(po)
        docs += out["added"]
        failed += out["failed"]
    return {"pos": pos, "docs": docs, "failed": failed}
