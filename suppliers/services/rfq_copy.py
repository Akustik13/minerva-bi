"""📋 Копія запиту постачальникам (RFQ): новий запит на основі старого — що запитуємо, форма й параметри, кількості,
файли, кому, лист.

Нічого не створюється, поки людина не збереже форму (`…/supplierrequest/new/?copy=<pk>` — `draft`); файли старого
запиту копіюються лише ті, що лишили (`copy_files`). Поля, які після копіювання зазвичай треба змінити, підсвічені:
`SupplierRequest.review` = {ключ поля: значення при копіюванні} — поле на перевірці, доки значення те саме, людина
не натисне «✓ так і лишити» на полі або «✓ Усе перевірено» (`settle`). Тема й текст листа, які склала Minerva,
не копіюються — складуться заново з нових даних; свої — копіюються, а старі номер, дата й назва в них стають
змінними {code} / {reply_by} / {title} (`templatize`).
"""
import datetime

from django.core.files.base import ContentFile
from django.utils import timezone

from suppliers.services import rfq

MUST, MAYBE = "must", "maybe"
# ключ поля → (рівень, назва, чому підсвічено); параметри форми — SPEC + ключ, рівень MUST
FIELDS = {
    "title": (MUST, "Що запитуємо", "Назва зі старого запиту — що запитуємо цього разу?"),
    "quantities": (MUST, "Кількості", "Кількості зі старого запиту."),
    "notes": (MUST, "Ще (вільним текстом)", "Вимоги зі старого запиту — ще актуальні?"),
    "files": (MUST, "Файли", "Файли зі старого запиту — ті самі версії? Непотрібні — «не брати», нові — додайте."),
    "subject": (MUST, "Тема листа", "Тема зі старого запиту."),
    "body": (MUST, "Текст листа", "Текст зі старого запиту — кількості й деталі в ньому теж старі."),
    "reply_by": (MAYBE, "Чекаємо відповідь до", "Нова дата: сьогодні + стільки ж днів, як у старому запиті."),
    "project": (MAYBE, "Проєкт", "Проєкт старого запиту — той самий?"),
    "group": (MAYBE, "Група / тема", "Група старого запиту — та сама?"),
}
SPEC = "spec:"
SPEC_WHY = "Параметр зі старого запиту."
REPLY_DAYS = 7          # старий запит без «чекаємо до»
REPLY_MAX_DAYS = 90
TITLE_MIN = 6           # коротку назву («Плата») у тексті не замінюємо змінною — надто легко зачепити інше


def norm(value) -> str:
    """Однаково для сервера й браузера (request_form.html → rqNorm): рядки без пробілів у кінці, без порожніх по краях."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "\n".join(sorted(norm(v) for v in value))
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def auto_subject(src) -> bool:
    """Тему склала Minerva (або порожня) — у копії складеться заново."""
    return not (src.subject or "").strip() or norm(src.subject) == norm(rfq.subject_for(src))


def auto_body(src) -> bool:
    return not (src.body or "").strip() or norm(src.body) == norm(rfq.build_body(src))


def templatize(text, src) -> tuple:
    """(текст, [замінені змінні]): старий номер → {code}, стара дата «чекаємо до» → {reply_by}, назва → {title}."""
    out, done = text or "", []
    title = (src.title or "").strip()
    if len(title) >= TITLE_MIN and title in out:
        out, done = out.replace(title, "{title}"), done + ["title"]
    if src.code and src.code in out:
        out, done = out.replace(src.code, "{code}"), done + ["code"]
    if src.reply_by:
        d = src.reply_by.strftime("%d.%m.%Y")
        if d in out:
            out, done = out.replace(d, "{reply_by}"), done + ["reply_by"]
    return out, done


def reply_by_for(src, today=None):
    """Сьогодні + стільки ж днів на відповідь, як дали в старому запиті (1…90), інакше + 7."""
    today = today or timezone.localdate()
    days = REPLY_DAYS
    if src.reply_by and src.created_at:
        days = min(max((src.reply_by - timezone.localdate(src.created_at)).days, 1), REPLY_MAX_DAYS)
    return today + datetime.timedelta(days=days)


def draft(src, user):
    """(незбережений запит-копія для форми, що сказати людині)."""
    from suppliers.models import SupplierRequest
    req = SupplierRequest(title=src.title, template=src.template, category_id=src.category_id,
                          project_id=src.project_id, group=src.group, specs=dict(src.specs or {}),
                          quantities=src.quantities, language=src.language, cc=src.cc, cc_contacts=src.cc_contacts,
                          reply_by=reply_by_for(src), owner=user, copied_from=src)
    if src.account_id and src.account and src.account.user_id == getattr(user, "pk", None):
        req.account = src.account
    replaced = []
    if not auto_subject(src):
        req.subject, done = templatize(src.subject, src)
        replaced += done
    if not auto_body(src):
        req.body, done = templatize(src.body, src)
        replaced += done
    return req, {"auto_subject": auto_subject(src), "auto_body": auto_body(src),
                 "replaced": [k for k in ("code", "reply_by", "title") if k in replaced]}


def recipients(src) -> dict:
    """Кому було: швидкий вибір у формі копії й контакт кожному. blocked — ⛔ «Не працювати» (у формі їх немає)."""
    from inventory.models import Supplier
    from suppliers.models import SupplierRequestRecipient as R
    rows = list(src.recipients.select_related("supplier"))
    ok = [r for r in rows if r.supplier.status != Supplier.STATUS_BLOCKED]
    answered = (R.REPLIED, R.QUOTED, R.DECLINED)
    return {
        "all": [r.supplier_id for r in ok],
        "quoted": [r.supplier_id for r in ok if r.state == R.QUOTED or r.price is not None or r.tiers],
        "answered": [r.supplier_id for r in ok if r.state in answered or r.replied_at],
        "chosen": [r.supplier_id for r in ok if r.chosen],
        "contacts": {r.supplier_id: (r.contact_id or -1) for r in ok},
        "blocked": [r.supplier.name for r in rows if r.supplier.status == Supplier.STATUS_BLOCKED],
    }


def _file_key(f) -> str:
    return f.sha256 or f.name


def values(req, files=None) -> dict:
    """Поточні значення полів, що можуть бути на перевірці (ключ → norm)."""
    specs = req.specs or {}
    out = {"title": req.title, "quantities": req.quantities, "notes": specs.get("_notes", ""),
           "subject": req.subject, "body": req.body,
           "reply_by": req.reply_by.isoformat() if req.reply_by else "",
           "project": str(req.project_id or ""), "group": req.group}
    for k, v in specs.items():
        if not str(k).startswith("_"):
            out[SPEC + str(k)] = v
    if files is not None:
        out["files"] = [_file_key(f) for f in files]
    return {k: norm(v) for k, v in out.items()}


def initial(req, files) -> dict:
    """Що підсвітити в щойно скопійованому запиті: непорожні поля з FIELDS і параметри форми. Тема й текст, які
    складе Minerva (у копії порожні), — ні."""
    return {k: v for k, v in values(req, files).items() if v and (k in FIELDS or k.startswith(SPEC))}


def settle(review, req, reviewed=()) -> dict:
    """Що й далі на перевірці після збереження: значення не змінилось і людина не сказала «так і лишити»."""
    now = values(req, list(req.files.all()) if req.pk else None)
    skip = set(reviewed or ())
    return {k: v for k, v in (review or {}).items() if k not in skip and now.get(k) == v}


def describe(review, req) -> list:
    """[{key, level, label, why, value}] для форми й сторінки запиту, у порядку полів форми."""
    labels = {f[0]: f[1] for f in rfq.fields_of(req.template)}
    order = ["title", "project", "group", "quantities", "reply_by"] + [SPEC + k for k in labels] + \
        ["notes", "files", "subject", "body"]
    keys = [k for k in order if k in (review or {})] + [k for k in (review or {}) if k not in order]
    out = []
    for k in keys:
        if k.startswith(SPEC):
            level, label, why = MUST, labels.get(k[len(SPEC):], k[len(SPEC):]), SPEC_WHY
        elif k in FIELDS:
            level, label, why = FIELDS[k]
        else:
            continue
        out.append({"key": k, "level": level, "label": label, "why": why, "value": review[k]})
    return out


def pending(req) -> list:
    """Ще не перевірено (лише чернетка): describe(...)."""
    from suppliers.models import SupplierRequest
    if req.status != SupplierRequest.DRAFT or not req.review:
        return []
    return describe(req.review, req)


def copy_files(src, req, user=None, skip=()) -> list:
    """Файли старого запиту → у новий (свої копії на диску: видалення в одному не зачепить інший), крім skip
    (pk «не брати»). → назви файлів, яких на диску вже немає."""
    from suppliers.models import SupplierRequestFile
    skip = {str(x) for x in skip or ()}
    missing = []
    for f in src.files.all():
        if str(f.pk) in skip:
            continue
        try:
            with f.file.open("rb") as fh:
                data = fh.read()
        except (OSError, ValueError):
            missing.append(f.name)
            continue
        new = SupplierRequestFile(request=req, name=f.name, size=len(data), sha256=f.sha256, uploaded_by=user)
        new.file.save(f.name, ContentFile(data), save=False)
        new.save()
    return missing
