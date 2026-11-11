"""
suppliers/services/cards.py — 🪪 візитки й флаєри поза постачальником: що показати на плитці, поділитися поштою
(чернетка з фото й vCard), внести в CRM клієнтом, прочитати AI кілька візиток разом, категорії з відповіді AI.

  match_categories(names)  — назви від AI → наші SupplierCategory (без значків, регістру, «Виробники плат (PCB)»);
  info(m, cats)            — плитка: фірма, людина, країна з прапорцем, категорії, що роблять, стан AI, нова категорія;
  card_data(m)             — поля візитки (прочитане AI / QR) з доповненням з картки постачальника;
  vcard(m)                 — .vcf (vCard 3.0) для телефону / Outlook;
  share(m, user)           — чернетка листа в основній скриньці людини: текст, фото обох боків, vCard;
  customer_matches(data)   — клієнти CRM, схожі на візитку (email, домен фірми, назва фірми);
  to_customer(m, user, …)  — новий клієнт CRM або дописати порожнє наявному: фото → «📎 Документи» клієнта,
                             нотатка «🪪 Візитка» в хронології, SupplierMedia.customer;
  to_read(user, scope)     — що прочитати AI разом: unread (AI ще не читав) / nocat (без категорії);
  read_many(ids, user, …)  — AI по черзі (фонове завдання сторінки «🪪 Візитки й флаєри»);
  add_category(m, user)    — запропонована AI нова категорія → SupplierCategory (та сама назва — наявна) → візитці;
  tile_prefs / save_tile_prefs — що показувати на плитках (кожна людина своє, UserProfile.ui_prefs["supplier_cards"]).
"""
import os
import re

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

READ_MAX = 40            # за одне натискання «🧠 Прочитати AI…»
PREFS_KEY = "supplier_cards"
# що можна сховати на плитці (ключ — клас spc-f-<ключ> у шаблоні); за замовчуванням видно все
TILE_FIELDS = [
    ("ai", "🧠 Стан AI"), ("company", "🏢 Фірма"), ("person", "👤 Людина й посада"), ("country", "🌍 Країна"),
    ("site", "🌐 Сайт"), ("cats", "🗂 Категорії"), ("newcat", "💡 Пропозиція нової категорії"),
    ("date", "📅 Дата й де взяли"), ("where", "🏭 / 👤 Куди внесено"), ("marks", "⭐ Позначки"),
    ("note", "📝 Наші примітки"), ("share", "✉️ Кнопка «Поділитись»"), ("crm", "👤 Кнопка «У CRM»"),
    ("vcard", "⬇ Кнопка «vCard»"),
]
# «Що роблять»: згорнуто (клік — розгорнути) / повністю / не показувати
DESC_MODES = [("collapsed", "згорнуто — розгорнути кліком"), ("full", "повністю"), ("off", "не показувати")]
_WORD_RE = re.compile(r"[^\w]+")
AI_LABELS = {"ai": "🧠 AI ✓", "qr": "🔳 з QR-коду", "failed": "⚠️ AI не прочитав", "none": "○ AI ще не читав"}


def _norm(s) -> str:
    return _WORD_RE.sub(" ", str(s or "").lower()).replace("_", " ").strip()


def match_categories(names, cats=None) -> list:
    """Назви категорій від AI → наші активні категорії: спершу точний збіг без значків і розділових знаків,
    потім одна назва містить іншу (лише від 4 літер — «PCB» не спрацює на будь-що)."""
    from suppliers.models import SupplierCategory
    if cats is None:
        cats = list(SupplierCategory.objects.filter(is_active=True))
    out = []
    for n in names or []:
        k = _norm(n)
        if not k:
            continue
        hit = next((c for c in cats if _norm(c.name) == k), None)
        if hit is None and len(k) >= 4:
            hit = next((c for c in cats if len(_norm(c.name)) >= 4 and (_norm(c.name) in k or k in _norm(c.name))),
                       None)
        if hit is not None and hit not in out:
            out.append(hit)
    return out


def flag(code) -> str:
    code = (code or "").upper()
    return "".join(chr(0x1F1E6 + ord(ch) - 65) for ch in code) if re.fullmatch(r"[A-Z]{2}", code) else ""


def ai_state(m) -> str:
    from suppliers.models import SupplierMedia
    if m.cost_usd:
        return "ai"
    if m.state == SupplierMedia.FAILED:
        return "failed"
    if (m.extracted or {}).get("from_qr"):
        return "qr"
    return "none"


def card_data(m) -> dict:
    """Поля візитки: прочитане (AI / QR); чого немає — з картки постачальника, якщо візитку вже внесено."""
    d = dict(m.extracted or {})
    s = m.supplier
    if s is not None:
        for k, v in (("name", s.name), ("website", s.website), ("email", s.email), ("phone", s.phone),
                     ("addr_street", s.addr_street), ("addr_zip", s.addr_zip), ("addr_city", s.addr_city),
                     ("addr_country", s.addr_country), ("description", getattr(s, "description", ""))):
            if v and not d.get(k):
                d[k] = v
    return d


def site_url(website) -> str:
    """«www.coax-connectors.com» / «https://…» → посилання для плитки; не схоже на сайт — ''."""
    w = str(website or "").strip()
    if re.match(r"^https?://[\w.-]+\.[a-z]{2,}", w, re.I):
        return w
    if re.match(r"^[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", w, re.I):
        return "https://" + w
    return ""


def _desc(text) -> str:
    """«Що роблять» без хвоста «Продукція: …» (той — у повному описі)."""
    return str(text or "").split("\nПродукція:")[0].strip()


def info(m, cats=None) -> dict:
    """Плитка «🪪 Візитки й флаєри»: фірма, людина, країна, категорії (постачальника → візитки → від AI), що роблять,
    стан AI, запропонована нова категорія (коли жодна наша не підійшла)."""
    from shop.countries import name as country_name
    d = card_data(m)
    country = (d.get("addr_country") or "").upper()
    chosen = list(m.supplier.categories.all()) if m.supplier_id else []
    chosen = chosen or list(m.categories.all())
    guessed = [] if chosen else match_categories(d.get("categories"), cats)
    new_cat = d.get("new_category") if isinstance(d.get("new_category"), dict) else {}
    key = ai_state(m)
    return {
        "company": d.get("name") or "", "person": d.get("contact_name") or "",
        "position": d.get("contact_position") or "",
        "country": country_name(country) if country else "", "flag": flag(country),
        "cats": chosen or guessed, "cats_guessed": bool(guessed),
        "new_category": new_cat if new_cat.get("name") and not (chosen or guessed) else {},
        "desc": _desc(d.get("description")),
        "site": site_url(d.get("website")), "site_label": re.sub(r"^(https?://)?(www\.)?", "", str(
            d.get("website") or ""), flags=re.I).rstrip("/")[:40],
        "ai": {"key": key, "label": AI_LABELS[key],
               "title": f"Прочитано AI · ${float(m.cost_usd):.3f}" if key == "ai" else
               (m.error or "AI не прочитав фото") if key == "failed" else
               "Поля заповнено з QR-коду, AI не запускали" if key == "qr" else "AI ще не читав це фото"},
    }


# ── vCard і «✉️ Поділитись» ──

def _vc(value) -> str:
    """Екранування значення vCard 3.0: \\ ; , і переноси рядка."""
    return (str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n").strip())


def vcard(m) -> str:
    d = card_data(m)
    person = (d.get("contact_name") or "").strip()
    company = (d.get("name") or "").strip()
    full = person or company or m.title or "Contact"
    parts = person.split()
    last, first = (parts[-1], " ".join(parts[:-1])) if len(parts) > 1 else (person, "")
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"N:{_vc(last)};{_vc(first)};;;", f"FN:{_vc(full)}"]
    if company:
        lines.append(f"ORG:{_vc(company)}")
    if d.get("contact_position"):
        lines.append(f"TITLE:{_vc(d['contact_position'])}")
    for kind, key in (("WORK,VOICE", "contact_phone"), ("CELL", "contact_mobile"), ("WORK,VOICE", "phone")):
        if d.get(key):
            lines.append(f"TEL;TYPE={kind}:{_vc(d[key])}")
    for key in ("contact_email", "email"):
        if d.get(key) and (key == "contact_email" or d.get(key) != d.get("contact_email")):
            lines.append(f"EMAIL;TYPE=INTERNET:{_vc(d[key])}")
    if d.get("website"):
        lines.append(f"URL:{_vc(d['website'])}")
    if any(d.get(k) for k in ("addr_street", "addr_city", "addr_zip", "addr_country")):
        lines.append("ADR;TYPE=WORK:;;{};{};;{};{}".format(*(_vc(d.get(k)) for k in (
            "addr_street", "addr_city", "addr_zip", "addr_country"))))
    note = " · ".join(x for x in (m.event, _desc(d.get("description"))) if x)
    if note:
        lines.append(f"NOTE:{_vc(note[:500])}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def file_stem(m) -> str:
    d = card_data(m)
    base = d.get("contact_name") or d.get("name") or m.title or f"card-{m.pk}"
    return (re.sub(r"[^\w.-]+", "_", base, flags=re.U).strip("_.") or f"card-{m.pk}")[:60]


def mail_text(m, link="") -> str:
    """Текст листа «✉️ Поділитись»: усе, що відомо з візитки, наші примітки й позначки."""
    d = card_data(m)
    i = info(m)
    head = f"{m.get_kind_display()}" + (f" · {m.event}" if m.event else "") + f" · {timezone.localtime(m.created_at):%d.%m.%Y}"
    rows = [("Фірма", d.get("name")),
            ("Контакт", " — ".join(x for x in (d.get("contact_name"), d.get("contact_position")) if x)),
            ("Email", ", ".join(dict.fromkeys(x for x in (d.get("contact_email"), d.get("email")) if x))),
            ("Телефон", ", ".join(dict.fromkeys(x for x in (d.get("contact_phone"), d.get("contact_mobile"),
                                                             d.get("phone")) if x))),
            ("Сайт", d.get("website")),
            ("Адреса", ", ".join(x for x in (d.get("addr_street"), " ".join(
                x for x in (d.get("addr_zip"), d.get("addr_city")) if x), i["country"]) if x)),
            ("Що роблять", _desc(d.get("description"))),
            ("Категорії", ", ".join(c.name for c in i["cats"])),
            ("Наші примітки", m.note), ("Позначки", m.marks_text())]
    body = [head, ""] + [f"{k}: {v}" for k, v in rows if v]
    if link:
        body += ["", f"У Minerva: {link}"]
    return "\n".join(body)


SHARE_MAX = 30                      # візиток в одному листі «✉️ Поділитись вибраними»
PHOTOS_MAX_BYTES = 18 * 1024 * 1024  # фото в листі разом — поштові сервери рідко приймають понад 20–25 МБ


def share(m, user, link=""):
    """Чернетка в основній скриньці людини (як «💾 Зберегти» в «Написати»): текст візитки, фото обох боків і .vcf.
    Кому — людина вписує сама. Немає скриньки — ValueError."""
    return share_many([m], user, {m.pk: link} if link else None)


def share_many(ms, user, links=None):
    """Одна чернетка з кількома візитками (до SHARE_MAX): текст кожної, фото обох боків — поки разом не більше
    PHOTOS_MAX_BYTES (решта — лише за посиланням у тексті), контакти — один .vcf (одна візитка — <ім'я>.vcf)."""
    from email_assistant import attachments
    from email_assistant.models import EmailAccount, EmailDraft
    ms = list(ms)[:SHARE_MAX]
    if not ms:
        raise ValueError("Виберіть візитки.")
    account = EmailAccount.objects.filter(user=user, is_active=True).order_by("-is_primary").first()
    if account is None:
        raise ValueError("У вас немає поштової скриньки в Minerva — підключіть її в «Пошта → Акаунти».")
    links = links or {}
    many = len(ms) > 1
    items, used, skipped = [], 0, 0
    for m in ms:
        stem = file_stem(m) + (f"-{m.pk}" if many else "")
        for f, side in ((m.image, ""), (m.image_back, "-back")):
            if not f:
                continue
            try:
                with f.open("rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if used + len(data) > PHOTOS_MAX_BYTES:
                skipped += 1
                continue
            used += len(data)
            ext = os.path.splitext(f.name)[1].lower() or ".jpg"
            items.append(attachments.store_file(f"{stem}{side}{ext}", data))
    vcf = "".join(vcard(m) for m in ms).encode("utf-8")
    items.append(attachments.store_file(f"{file_stem(ms[0])}.vcf" if not many else f"contacts-{len(ms)}.vcf", vcf,
                                        "text/vcard"))
    if many:
        events = {m.event for m in ms}
        subject = f"Візитки ({len(ms)})" + (f": {ms[0].event}" if len(events) == 1 and ms[0].event else "")
        body = "\n\n————————\n\n".join(f"{n}. " + mail_text(m, links.get(m.pk, "")) for n, m in enumerate(ms, 1))
    else:
        d = card_data(ms[0])
        who = " — ".join(x for x in (d.get("name"), d.get("contact_name")) if x) or ms[0].title or \
            ms[0].get_kind_display()
        subject, body = f"Візитка: {who}", mail_text(ms[0], links.get(ms[0].pk, ""))
    if skipped:
        body += ("\n\nФото не всіх візиток вклалися в розмір листа — решта в Minerva за посиланнями вище.")
    return EmailDraft.objects.create(account=account, subject=subject[:500], to_emails=[], body=body,
                                     attachments=items, source=EmailDraft.SOURCE_MANUAL)


# ── 👤 Клієнт CRM з візитки ──

def customer_fields(data) -> dict:
    """Поля форми візитки → клієнт CRM: людина — контактна особа (немає — фірма), email і телефон — спершу людини."""
    from suppliers.services import match
    email = match.addr(data.get("contact_email")) or match.addr(data.get("email"))
    phone = data.get("contact_mobile") or data.get("contact_phone") or data.get("phone") or ""
    country = (data.get("addr_country") or "").strip().upper()
    return {"name": (data.get("contact_name") or "").strip()[:255], "company": (data.get("name") or "").strip()[:255],
            "email": email, "phone": phone.strip()[:50], "addr_street": (data.get("addr_street") or "").strip()[:300],
            "addr_zip": (data.get("addr_zip") or "").strip()[:20], "addr_city": (data.get("addr_city") or "").strip()[:100],
            "country": country if re.fullmatch(r"[A-Z]{2}", country) else ""}


def customer_matches(data, limit=5) -> list:
    """Клієнти CRM, схожі на візитку: той самий email → той самий домен фірми (не поштовик) → та сама назва фірми."""
    from crm.models import Customer
    from suppliers.services import match
    out, seen = [], set()

    def add(qs, why):
        for c in qs:
            if c.pk not in seen and len(out) < limit:
                seen.add(c.pk)
                out.append((c, why))

    emails = [e for e in dict.fromkeys(match.addr(data.get(k)) for k in ("contact_email", "email")) if e]
    for e in emails:
        add(Customer.objects.filter(email__iexact=e)[:3], "той самий email")
    for e in emails:
        dom = match.domain_of(e)
        if dom and not match.is_free(dom):
            add(Customer.objects.filter(email__iendswith="@" + dom)[:3], f"той самий домен {dom}")
    company = (data.get("name") or "").strip()
    key = match.name_key(company)
    words = sorted((w for w in re.findall(r"\w{4,}", company) if w.lower() not in {"limited", "company", "group",
                                                                                   "technology", "electronic"}),
                   key=len, reverse=True)
    if key and words:
        cand = Customer.objects.filter(company__icontains=words[0])[:30]
        add([c for c in cand if match.name_key(c.company) == key], "та сама фірма")
    return out


def _copy_photos(m, customer) -> list:
    """Фото візитки → media/customers/<pk>/ («📎 Документи» клієнта); уже скопійоване — не вдруге."""
    folder = os.path.join(str(settings.MEDIA_ROOT), "customers", str(customer.pk))
    os.makedirs(folder, exist_ok=True)
    out = []
    for f, side in ((m.image, ""), (m.image_back, "-зворот")):
        if not f:
            continue
        ext = os.path.splitext(f.name)[1].lower() or ".jpg"
        name = f"Візитка-{m.pk}{side}{ext}"
        dest = os.path.join(folder, name)
        if os.path.exists(dest):
            continue
        try:
            with f.open("rb") as src, open(dest, "wb") as fh:
                fh.write(src.read())
            out.append(name)
        except OSError:
            continue
    return out


def to_customer(m, user, data, customer=None) -> dict:
    """Візитка → клієнт CRM. customer=None — новий (той самий email уже є — він, без дубля); наявному — лише
    порожні поля. Фото → «📎 Документи» клієнта, нотатка «🪪 Візитка» в хронології, візитка пам'ятає клієнта.
    → {customer, created, filled, files}"""
    from crm.models import Customer, CustomerTimeline
    from suppliers.models import SupplierMedia
    f = customer_fields(data)
    created = False
    if customer is None:
        customer = Customer.objects.filter(email__iexact=f["email"]).first() if f["email"] else None
        if customer is None:
            if not (f["name"] or f["company"] or f["email"]):
                raise ValueError("На візитці немає ні імені, ні фірми, ні email — заповніть поля вище.")
            note = "\n".join(x for x in (f"{m.get_kind_display()}" + (f" · {m.event}" if m.event else ""),
                                         _desc(data.get("description")), m.note) if x)
            customer = Customer.objects.create(
                name=f["name"] or f["company"] or f["email"], company=f["company"], email=f["email"], phone=f["phone"],
                addr_street=f["addr_street"], addr_zip=f["addr_zip"], addr_city=f["addr_city"], country=f["country"],
                source="card", notes=note[:2000])
            created = True
    filled = []
    if not created:
        for k in ("company", "email", "phone", "addr_street", "addr_zip", "addr_city", "country"):
            if f.get(k) and not getattr(customer, k):
                setattr(customer, k, f[k])
                filled.append(k)
        if filled:
            customer.save(update_fields=filled + ["updated_at"])
    files = _copy_photos(m, customer)
    who = " — ".join(x for x in (f["name"], f["company"]) if x)
    CustomerTimeline.objects.create(
        customer=customer, user=user, event_type="note",
        title=(f"{m.get_kind_display()}" + (f" · {m.event}" if m.event else "") + (f": {who}" if who else ""))[:300],
        body=mail_text(m) + (f"\n\nФото — у «📎 Документи»: {', '.join(files)}" if files else ""))
    SupplierMedia.objects.filter(pk=m.pk).update(customer=customer)
    m.customer = customer
    return {"customer": customer, "created": created, "filled": filled, "files": files}


# ── 🧠 AI для кількох візиток ──

def to_read(user, scope, can_all=True):
    """unread — AI ще не читав (поля лише з QR теж); nocat — без жодної категорії (і перечитати). Уже внесені
    (постачальник чи клієнт CRM) — ні. Без права змінювати всі — лише свої фото."""
    from suppliers.models import SupplierMedia
    qs = SupplierMedia.objects.exclude(image="").filter(supplier=None, customer=None)
    if not can_all:
        qs = qs.filter(created_by=user)
    if scope == "nocat":
        qs = qs.filter(categories=None)
    else:
        qs = qs.filter(cost_usd=0)
    return qs.order_by("-created_at")


def read_many(ids, user, model) -> dict:
    """AI по черзі; три збої поспіль (ліміт AI, немає ключа…) — зупинитись."""
    from suppliers.models import SupplierMedia
    from suppliers.services import ai
    done = failed = streak = 0
    cost = 0.0
    error = ""
    for m in SupplierMedia.objects.filter(pk__in=list(ids)[:READ_MAX]):
        try:
            ai.card(m, user=user, model=model)
            done += 1
            streak = 0
            cost += float(m.cost_usd or 0)
        except ai.AIError as e:
            failed += 1
            streak += 1
            error = str(e)
            if streak >= 3:
                break
    return {"done": done, "failed": failed, "cost": cost, "error": error}


# ── 🗑 Видалити ──

def delete(m):
    """Фото обох боків, оригінали до правок і саму візитку; постачальник / клієнт CRM, створені з неї, лишаються."""
    for f in (m.image, m.image_back, m.image_original, m.image_back_original):
        if f:
            f.delete(save=False)
    m.delete()


# ── 📊 Статистика ──

NO_EVENT = "без події"


def event_key(event) -> str:
    return " ".join(str(event or "").lower().split())


def stats(user=None) -> dict:
    """📊 По подіях («Де взяли»: виставка, зустріч…) і разом: візиток, людей (ім'я / email), фірм (назва без форми),
    🏭 внесено постачальником, 👤 клієнтом CRM, ще не внесено, прочитав AI, позначки, основні країни й категорії,
    перша й остання дата. Лише читання бази, без AI."""
    from collections import Counter

    from shop.countries import name as country_name
    from suppliers.models import SupplierMedia
    from suppliers.services import match

    def blank(label, key):
        return {"event": label, "key": key, "cards": 0, "people": set(), "companies": set(), "suppliers": set(),
                "customers": set(), "new": 0, "ai": 0, "countries": Counter(), "cats": Counter(), "marks": Counter(),
                "first": None, "last": None}

    groups, total = {}, blank("Усього", "")
    marks = dict(SupplierMedia.MARKS)
    qs = (SupplierMedia.objects.select_related("supplier").prefetch_related("categories", "supplier__categories")
          .order_by("created_at"))
    for m in qs:
        key = event_key(m.event)
        g = groups.setdefault(key, blank((m.event or "").strip() or NO_EVENT, key or "-"))
        d = card_data(m)
        person = (d.get("contact_email") or d.get("contact_name") or "").strip().lower()
        company = match.name_key(d.get("name")) or (d.get("name") or "").strip().lower()
        cats = list(m.supplier.categories.all()) if m.supplier_id else list(m.categories.all())
        day = timezone.localtime(m.created_at).date()
        for x in (g, total):
            x["cards"] += 1
            if person:
                x["people"].add(person)
            if company:
                x["companies"].add(company)
            if m.supplier_id:
                x["suppliers"].add(m.supplier_id)
            if m.customer_id:
                x["customers"].add(m.customer_id)
            if not (m.supplier_id or m.customer_id):
                x["new"] += 1
            if m.cost_usd:
                x["ai"] += 1
            if d.get("addr_country"):
                x["countries"][d["addr_country"].upper()] += 1
            for c in cats:
                x["cats"][f"{c.icon} {c.name}"] += 1
            for k in m.marks or []:
                if k in marks:
                    x["marks"][marks[k]] += 1
            x["first"] = min(x["first"] or day, day)
            x["last"] = max(x["last"] or day, day)

    def done(x):
        return {**x, "people": len(x["people"]), "companies": len(x["companies"]), "suppliers": len(x["suppliers"]),
                "customers": len(x["customers"]),
                "countries": [(country_name(c), n) for c, n in x["countries"].most_common(3)],
                "cats": x["cats"].most_common(3), "marks": x["marks"].most_common(4)}
    events = sorted((done(g) for g in groups.values()), key=lambda g: g["last"] or timezone.localdate(), reverse=True)
    return {"events": events, "total": done(total)}


# ── 💡 Нова категорія від AI ──

def add_category(m, user):
    """Категорія, яку запропонував AI (extracted.new_category), → SupplierCategory (така назва вже є — вона) →
    категорії візитки; візитку вже внесено постачальником — і йому. → (категорія, створено?)"""
    from suppliers.models import SupplierCategory
    proposal = (m.extracted or {}).get("new_category") or {}
    name = " ".join(str(proposal.get("name") or "").split())[:120]
    if not name:
        raise ValueError("AI не пропонував нової категорії для цього фото.")
    found = next((c for c in SupplierCategory.objects.all() if _norm(c.name) == _norm(name)), None)
    created = found is None
    cat = found or SupplierCategory.objects.create(name=name, icon=str(proposal.get("icon") or "🏭")[:8],
                                                   description=str(proposal.get("description") or "")[:500])
    if not cat.is_active:
        SupplierCategory.objects.filter(pk=cat.pk).update(is_active=True)
    m.categories.add(cat)
    if m.supplier_id:
        m.supplier.categories.add(cat)
    return cat, created


def tile_prefs(user) -> dict:
    """{hide: [ключі TILE_FIELDS], desc: collapsed | full | off} людини; невідоме — відкидається."""
    profile = getattr(user, "profile", None)
    raw = ((getattr(profile, "ui_prefs", None) or {}) if profile else {}).get(PREFS_KEY) or {}
    keys = {k for k, _label in TILE_FIELDS}
    hide = [k for k in (raw.get("hide") or []) if k in keys] if isinstance(raw, dict) else []
    desc = raw.get("desc") if isinstance(raw, dict) else None
    return {"hide": hide, "desc": desc if desc in dict(DESC_MODES) else "collapsed"}


def save_tile_prefs(user, hide=None, desc=None, reset=False) -> dict:
    """Записати вибір людини; решта ui_prefs (тема, меню, панелі…) не змінюється — рядок профілю під блокуванням."""
    from django.db import transaction

    from core.models import UserProfile
    with transaction.atomic():
        profile, _ = UserProfile.objects.select_for_update().get_or_create(user=user)
        prefs = dict(profile.ui_prefs or {})
        if reset:
            prefs.pop(PREFS_KEY, None)
        else:
            keys = {k for k, _label in TILE_FIELDS}
            prefs[PREFS_KEY] = {"hide": [k for k in dict.fromkeys(hide or []) if k in keys],
                                "desc": desc if desc in dict(DESC_MODES) else "collapsed"}
        profile.ui_prefs = prefs
        profile.save(update_fields=["ui_prefs"])
    if hasattr(user, "profile"):
        user.profile.ui_prefs = prefs
    return tile_prefs(user)


def card_counts(user, can_all=True) -> dict:
    return {"unread": to_read(user, "unread", can_all).count(), "nocat": to_read(user, "nocat", can_all).count()}


def search_customers(q, limit=15) -> list:
    from crm.models import Customer
    q = " ".join(str(q or "").split())[:80]
    if len(q) < 2:
        return []
    qs = Customer.objects.filter(Q(company__icontains=q) | Q(name__icontains=q) | Q(email__icontains=q))
    return [{"id": c.pk, "text": " — ".join(x for x in (c.company, c.name, c.email) if x)}
            for c in qs.order_by("company", "name")[:limit]]
