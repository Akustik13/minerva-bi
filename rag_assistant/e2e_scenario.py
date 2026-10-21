"""
Наскрізний сценарій Minerva ↔ приватний RAG на синтетичних даних (rag_assistant/e2e/README.md).

Повторюваний: кожен прогін починає з вихідного стану (restore_baseline), працює з власним листом прогону
і в кінці повертає дані як були. Результат — JSON-звіт без ключів і текстів листів, щоб порівнювати прогони
(час пошуку, оцінки, синхронізація) після змін на боці RAG.
Лише для tabele.settings_e2e (перевіряє команда knowledge_e2e); тут — логіка, яку також покривають тести.
"""
import os
import statistics
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

DOMAIN = "minerva-e2e.test"
SALES = f"vertrieb@{DOMAIN}"
SERVICE = f"service@{DOMAIN}"
CUSTOMER_EMAIL = "einkauf@muster-antennen.test"
SUPPLIER_EMAIL = "info@beispiel-kabel.test"
PROJECT_NAME = "Antennen Muster 2026"
DEAD_URL = "http://127.0.0.1:9"
LOCAL_RAG = "http://127.0.0.1:8788"
REPORT_VERSION = 1
BASE = datetime(2026, 10, 12, 8, 0, tzinfo=dt_tz.utc)
CAB_OLD, CAB_NEW = "14.11.2026", "21.11.2026"
STEPS = ("new-mail", "edit", "reassign", "restore-owner", "deactivate", "activate",
         "exclude", "include", "delete", "undelete", "purge")
ACCOUNT_STEPS = ("reassign", "restore-owner", "deactivate", "activate")

# (ключ, скринька, папка, відправник, кому, тема, текст, вкладення, розсилка)
MAILS = [
    ("m1", SALES, "INBOX", CUSTOMER_EMAIL, [SALES], "Anfrage Antenne ANT-2400, 500 Stück",
     "Guten Tag,\nbitte senden Sie uns ein Angebot für 500 Stück ANT-2400. "
     "Der Liefertermin KW 46 ist für unser Projekt zwingend.\nMit freundlichen Grüßen\nEinkauf Muster Antennen",
     None, False),
    ("m2", SALES, "Sent", SALES, [CUSTOMER_EMAIL], "Re: Anfrage Antenne ANT-2400, 500 Stück",
     "Sehr geehrte Damen und Herren,\nwir bieten 500 Stück ANT-2400 zu 12,40 EUR je Stück an. "
     "Die Lieferung in KW 46 sagen wir verbindlich zu.\nMit freundlichen Grüßen\nVertrieb",
     None, False),
    ("m3", SALES, "INBOX", CUSTOMER_EMAIL, [SALES], "Re: Anfrage Antenne ANT-2400, 500 Stück",
     "Danke für das Angebot. Anbei unsere Spezifikation zur Prüfung.",
     ("spezifikation-ant-2400.txt", "Spezifikation ANT-2400\nFrequenzbereich: 2,4 GHz\nGewinn: 5 dBi\n"
                                    "Steckverbinder: SMA-Buchse\nSchutzart: IP67\n"), False),
    ("m4", SALES, "INBOX", SUPPLIER_EMAIL, [SALES], "Lieferverzug Kabel CAB-7",
     f"Hallo,\ndie Lieferung der Kabel CAB-7 verschiebt sich auf den {CAB_OLD}. Wir bitten um Entschuldigung.",
     None, False),
    ("m5", SALES, "INBOX", f"kollege@{DOMAIN}", [SALES], "Intern: Rabatt Muster Antennen",
     "Für Muster Antennen gilt maximal 8 % Rabatt, Zahlungsziel 30 Tage.", None, False),
    ("m6", SALES, "INBOX", "news@shop-e2e.test", [SALES], "Wochenangebote",
     "Diese Woche: Rabatte auf Steckverbinder.", None, True),
    ("m7", SERVICE, "INBOX", CUSTOMER_EMAIL, [SERVICE], "Kalibrierprotokoll fehlt",
     "Für das Messgerät mit Seriennummer SN-77821 fehlt das Kalibrierprotokoll.", None, False),
]
BASE_KEYS = [m[0] for m in MAILS]
INDEXED_KEYS = ["m1", "m2", "m3", "m4", "m5", "m7"]          # m6 — розсилка, не індексується
ADDED_KEY = "m8"                                             # лист кроку new-mail (сценарій: свій для прогону)
ADDED_MAIL = (SALES, "INBOX", CUSTOMER_EMAIL, [SALES], "Re: Anfrage Antenne ANT-2400, 500 Stück",
              "Können Sie die Lieferung auf KW 45 vorziehen? Einen Expresszuschlag würden wir akzeptieren. "
              "Anbei der Prüfbericht.",
              ("pruefbericht-vibration.txt", "Prüfbericht Vibrationstest ANT-2400\n"
                                             "Frequenz 10 bis 500 Hz, drei Achsen: bestanden\n"), False)


# ── Дані ─────────────────────────────────────────────────────────────────────

def mid(key):
    return f"<{key}@{DOMAIN}>"


def message(key):
    from email_assistant.models import EmailMessage
    return EmailMessage.objects.filter(message_id=mid(key)).first()


def doc_id(key):
    m = message(key)
    return f"email:{m.pk}" if m else None


def users():
    return User.objects.get(username="e2e_anna"), User.objects.get(username="e2e_boris")


def _user(username):
    user = User.objects.filter(username=username).first()
    return user or User.objects.create_user(username, f"{username}@{DOMAIN}", None, is_staff=True)


def create_mail(key, spec):
    """Синтетичний лист через save_message (єдиний шлях збереження пошти). None — уже є."""
    from email_assistant.models import EmailAccount, EmailMessage
    from email_assistant.sync import save_message
    mailbox, folder, sender, to, subject, body, attachment, newsletter = spec
    if message(key) is not None:
        return None
    account = EmailAccount.objects.get(email_address=mailbox)
    attachments = []
    if attachment:
        name, content = attachment
        rel = f"email_attachments/e2e/{key}/{name}"
        path = Path(settings.MEDIA_ROOT) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        attachments = [{"name": name, "content_type": "text/plain", "size": path.stat().st_size, "file_path": rel}]
    base = key in BASE_KEYS
    uid = 1000 + int(key[1:]) if base else \
        max(2000, (EmailMessage.objects.filter(account=account).aggregate(m=Max("imap_uid"))["m"] or 0) + 1)
    refs = [mid("m1")] if subject.startswith("Re: Anfrage") else []
    return save_message(account, {
        "uid": uid, "message_id": mid(key), "in_reply_to": refs[0] if refs else "", "references": refs,
        "subject": subject, "from_email": sender, "from_name": "", "to_emails": to, "cc_emails": [],
        "body_text": body, "body_html": "", "attachments": attachments, "is_read": False,
        "list_unsubscribe": newsletter,
        "sent_at": BASE + timedelta(hours=int(key[1:])) if base else timezone.now()}, folder)


def seed():
    """Користувачі, скриньки, клієнт, постачальник, проєкт, листи — ідемпотентно. Токен пише команда."""
    from config.models import SystemSettings
    from crm.models import Customer
    from email_assistant.models import EmailAccount, EmailSettings
    from inventory.models import Supplier
    from projects import services
    from projects.models import Project
    from rag_assistant import knowledge
    from rag_assistant.models import KnowledgeSettings, RagSettings

    s = SystemSettings.objects.get_or_create(pk=1)[0]
    s.is_onboarding_complete = True
    s.save()
    if not User.objects.filter(username="e2e_admin").exists():
        User.objects.create_superuser("e2e_admin", f"admin@{DOMAIN}", None)   # пароль не задано
    anna, boris = _user("e2e_anna"), _user("e2e_boris")
    for user, address in ((anna, SALES), (boris, SERVICE)):
        EmailAccount.objects.get_or_create(email_address=address, defaults=dict(
            user=user, imap_host="imap.e2e.invalid", smtp_host="smtp.e2e.invalid",
            imap_username=address, imap_password="-", smtp_username=address, smtp_password="-",
            imap_folder_inbox="INBOX", imap_folder_sent="Sent", is_primary=True))
        es = EmailSettings.get_for_user(user)
        es.deadline_detection = es.telegram_notify_new = es.mark_read_on_server = False
        es.save()
    Customer.objects.get_or_create(email=CUSTOMER_EMAIL, defaults={"name": "Muster Antennen GmbH"})
    Supplier.objects.get_or_create(name="Beispiel Kabel AG", defaults={"email": SUPPLIER_EMAIL})
    ks = KnowledgeSettings.get()
    ks.enabled, ks.include_attachments = True, True
    ks.private_base_url = ks.private_base_url or LOCAL_RAG   # не успадковувати адресу робочого RAG
    ks.save()
    rs = RagSettings.get()
    rs.base_url, rs.enabled = "http://127.0.0.1:8787", False
    rs.save()
    created = [create_mail(m[0], m[1:]) for m in MAILS]
    if not Project.objects.filter(name=PROJECT_NAME).exists():
        services.create_project(name=PROJECT_NAME, user=anna, threads=[message("m1").thread])
    result = knowledge.refresh_all()
    return {"created": sum(1 for c in created if c), "events": result["events"], "anna": anna, "boris": boris}


def restore_baseline():
    """Вихідний стан: власник і активність скриньки, гілка не приватна, дата CAB-7, лист не в кошику,
    базові листи на місці, листи попередніх прогонів видалено. Повертає список виправлень."""
    from email_assistant.models import EmailAccount, EmailMessage, EmailThread
    from rag_assistant import knowledge
    from rag_assistant.models import KnowledgeSettings
    fixes = []
    anna, _ = users()
    with transaction.atomic():
        KnowledgeSettings.objects.filter(pk=1).update(rag_failures=0, rag_down_until=None)
        sales = EmailAccount.objects.get(email_address=SALES)
        if sales.user_id != anna.pk or not sales.is_active:
            sales.user, sales.is_active = anna, True
            sales.save()
            fixes.append("скринька vertrieb: власник anna, активна")
        extra = EmailMessage.objects.filter(message_id__endswith=f"@{DOMAIN}>").exclude(
            message_id__in=[mid(k) for k in BASE_KEYS])
        for m in extra:
            fixes.append(f"видалено лист попереднього прогону {m.message_id}")
            m.delete()
        for key, *spec in MAILS:
            if create_mail(key, spec) is not None:
                fixes.append(f"відновлено лист {key}")
        m4 = message("m4")
        if m4.body_text != MAILS[3][6]:
            m4.body_text = MAILS[3][6]
            m4.save()
            fixes.append("m4: дата CAB-7 повернута")
        if m4.thread_id and EmailThread.objects.filter(pk=m4.thread_id, knowledge_excluded=True).exists():
            EmailThread.objects.filter(pk=m4.thread_id).update(knowledge_excluded=False)
            fixes.append("гілка CAB-7 не приватна")
        m5 = message("m5")
        if m5.is_deleted:
            m5.is_deleted = False
            m5.save(update_fields=["is_deleted"])
            fixes.append("m5 повернуто з кошика")
    knowledge.touch_account(EmailAccount.objects.get(email_address=SALES).pk)   # синхронно, а не чекати потік
    knowledge.touch_thread(message("m4").thread_id)
    return fixes


# ── Кроки ────────────────────────────────────────────────────────────────────

def last_seq():
    from rag_assistant.models import KnowledgeChange
    return KnowledgeChange.objects.order_by("-id").values_list("id", flat=True).first() or 0


def apply_step(step, added_key=ADDED_KEY, wait=True):
    """Зміна даних для кроку сценарію. → (опис, нових подій стрічки)."""
    from email_assistant.models import EmailAccount, EmailThread
    if step not in STEPS:
        raise ValueError(f"Крок: {', '.join(STEPS)}")
    before = last_seq()
    sales = EmailAccount.objects.get(email_address=SALES)
    with transaction.atomic():
        if step == "new-mail":
            m = create_mail(added_key, ADDED_MAIL)
            note = f"лист {m.pk if m else '(уже є)'}: «Expresszuschlag», вкладення Prüfbericht"
        elif step == "edit":
            m = message("m4")
            old, new = (CAB_OLD, CAB_NEW) if CAB_OLD in m.body_text else (CAB_NEW, CAB_OLD)
            m.body_text = m.body_text.replace(old, new)
            m.save()
            note = f"лист {m.pk}: дата CAB-7 {old} → {new}"
        elif step in ("reassign", "restore-owner"):
            sales.user = User.objects.get(username="e2e_boris" if step == "reassign" else "e2e_anna")
            sales.save()
            note = f"власник {SALES} → {sales.user.username}"
        elif step in ("deactivate", "activate"):
            sales.is_active = step == "activate"
            sales.save()
            note = f"{SALES} активна: {sales.is_active}"
        elif step in ("exclude", "include"):
            thread = EmailThread.objects.get(pk=message("m4").thread_id)
            thread.knowledge_excluded = step == "exclude"
            thread.save(update_fields=["knowledge_excluded"])
            note = f"гілка {thread.pk} (CAB-7) приватна: {thread.knowledge_excluded}"
        elif step in ("delete", "undelete"):
            m = message("m5")
            m.is_deleted = step == "delete"
            m.save(update_fields=["is_deleted"])
            note = f"лист {m.pk} (Rabatt) у кошику: {m.is_deleted}"
        else:   # purge
            m = message(added_key)
            note = f"лист {m.pk} (Expresszuschlag + вкладення) видалено з БД" if m else f"лист {added_key} відсутній"
            if m:
                m.delete()
    if wait and step in ACCOUNT_STEPS:
        deadline = time.monotonic() + 10   # документи скриньки оновлює фоновий потік після commit
        while time.monotonic() < deadline and last_seq() == before:
            time.sleep(0.2)
        time.sleep(0.5)
    return note, last_seq() - before


# ── RAG ──────────────────────────────────────────────────────────────────────

def search(user, query, project=False):
    """Пошук як у Minerva (retrieval.start + poll) з перевіркою hits. Тексти листів у результат не йдуть."""
    from projects.models import Project
    from rag_assistant import retrieval
    project_id = Project.objects.get(name=PROJECT_NAME).pk if project else None
    t = time.monotonic()
    job = retrieval.start(user, query, purpose="history", project_id=project_id, wait_ms=retrieval.WAIT_EXTENDED_MS)
    while not job.is_terminal and time.monotonic() - t < 60:
        time.sleep(max(0.3, job.poll_after_ms / 1000))
        job = retrieval.poll(job)
    return {
        "user": user.username, "query": query, "project": project, "status": job.status,
        "seconds": round(time.monotonic() - t, 2), "message": job.message, "dropped": job.dropped,
        "ids": list(dict.fromkeys(h["document_id"] for h in job.hits)),
        "hits": [{"document_id": h["document_id"], "document_revision": h.get("document_revision"),
                  "score": h.get("score"), "attachment": (h.get("attachment") or {}).get("name")} for h in job.hits],
        "_texts": {h["document_id"]: h["text"] for h in job.hits},
    }


def raw_search(user_id, mailbox_ids, query):
    """Прямо в RAG, в обхід фільтрів Minerva — перевірка ACL на боці RAG. → список document_id."""
    from rag_assistant import retrieval
    from rag_assistant.knowledge import instance_id
    from rag_assistant.models import KnowledgeSettings
    s = KnowledgeSettings.get()
    ctx = {"source_instance_id": instance_id(), "generation": str(s.generation), "user_id": user_id}
    body = {"source_instance_id": ctx["source_instance_id"], "generation": ctx["generation"], "query": query,
            "types": ["email"], "filters": {"mailbox_ids": mailbox_ids, "user_id": user_id}, "limit": 20,
            "wait_ms": 8000}
    status, data = retrieval._request("POST", "/v2/retrieval/jobs", s=s, body=body,
                                      idempotency_key=f"e2e-acl:{uuid.uuid4().hex}", wait_ms=8000)
    t = time.monotonic()
    while status != 200 and data.get("status") in ("queued", "running") and time.monotonic() - t < 60:
        time.sleep(1)
        _, data = retrieval._request("GET", f"/v2/jobs/{data['job_id']}", s=s, params=ctx)
    result = data if status == 200 else (data.get("result") or {})
    return sorted({h["document_id"] for h in result.get("hits", [])})


def capabilities_index():
    from rag_assistant import retrieval
    _, boris = users()
    r = retrieval.capabilities(boris, force=True)
    return (r.get("data") or {}).get("index") or {"error": r.get("message")}


def unsynced():
    """Документи, статус RAG яких не відповідає чинній ревізії (fed → indexed, tombstone → deleted)."""
    from rag_assistant.models import KnowledgeDoc
    out = []
    for d in KnowledgeDoc.objects.order_by("obj_id"):
        want = KnowledgeDoc.INDEXED if d.fed else KnowledgeDoc.DELETED
        if d.status_revision != d.revision or d.state != want:
            out.append({"document_id": f"email:{d.obj_id}", "revision": d.revision, "state": d.state,
                        "status_revision": d.status_revision, "error": d.error[:120]})
    return out


def wait_sync(timeout=240):
    target, t = last_seq(), time.monotonic()
    idx = {}
    while time.monotonic() - t < timeout:
        idx = capabilities_index()
        if (idx.get("applied_seq") or 0) >= target and not unsynced():
            return {"ok": True, "target_seq": target, "seconds": round(time.monotonic() - t, 1),
                    "docs": idx.get("docs"), "chunks": idx.get("chunks")}
        time.sleep(2)
    return {"ok": False, "target_seq": target, "seconds": round(time.monotonic() - t, 1),
            "applied_seq": idx.get("applied_seq"), "last_error": idx.get("last_error"), "unsynced": unsynced()}


def fallback_checks(simulate=True):
    """Minerva без RAG: пошук швидко каже «недоступний», запобіжник вмикається, пошта й стрічка працюють.
    simulate — тимчасово мертва адреса RAG (потім повертається). → [(назва, ok, факт)]."""
    from django.test import Client

    from api.models import APIKey
    from rag_assistant import retrieval
    from rag_assistant.models import KnowledgeSettings, RetrievalJob
    saved_url = KnowledgeSettings.get().private_base_url
    KnowledgeSettings.objects.filter(pk=1).update(
        rag_failures=0, rag_down_until=None, **({"private_base_url": DEAD_URL} if simulate else {}))
    anna, _ = users()
    out = []
    try:
        for n in range(1, 5):
            t = time.monotonic()
            job = retrieval.start(anna, "Liefertermin ANT-2400", wait_ms=retrieval.WAIT_AUTO_MS)
            took = round(time.monotonic() - t, 1)
            out.append((f"пошук {n} без RAG → unavailable < 10 с", job.status == RetrievalJob.UNAVAILABLE and took < 10,
                        f"{job.status}, {took} с, {job.message}"))
        until = retrieval.breaker_until()
        out.append(("запобіжник після 3 збоїв", until is not None, str(until)))
        m = message("m2")
        client = Client()
        client.force_login(anna)
        history = client.get(f"/email/message/{m.pk}/history/")
        rag = client.post(f"/email/message/{m.pk}/rag-search/")
        key = APIKey.objects.filter(name="rag-e2e").first()
        feed = client.get("/api/v1/knowledge/changes/", {"after": 0, "limit": 1},
                          HTTP_AUTHORIZATION=f"Token {key.key}") if key else None
        out.append(("«📜 Історія» листа (лише Minerva)",
                    history.status_code == 200 and "events" in history.json(), history.status_code))
        out.append(("«🔎 Доповнити через RAG» → чесне «недоступний»",
                    rag.status_code == 200 and rag.json().get("status") == RetrievalJob.UNAVAILABLE,
                    rag.json().get("status") if rag.status_code == 200 else rag.status_code))
        out.append(("стрічка /api/v1/knowledge/changes/", feed is not None and feed.status_code == 200,
                    feed.status_code if feed is not None else "немає токена rag-e2e"))
    finally:
        KnowledgeSettings.objects.filter(pk=1).update(
            rag_failures=0, rag_down_until=None, **({"private_base_url": saved_url} if simulate else {}))
    return out


# ── Сценарій ─────────────────────────────────────────────────────────────────

class Scenario:
    """Кроки 1–15 з rag_assistant/e2e/README.md. log — функція виводу рядка."""

    def __init__(self, log=print, outage=False):
        self.log, self.outage = log, outage
        self.run_id = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        self.added_key = "r" + self.run_id.replace("-", "")
        self.checks, self.searches, self.syncs = [], [], []
        self.t0 = time.monotonic()

    # ── запис ────────────────────────────────────────────────────────────────
    def check(self, step, name, ok, actual):
        self.checks.append({"step": step, "name": name, "ok": bool(ok), "actual": actual,
                            "at_s": round(time.monotonic() - self.t0, 1)})
        self.log(f"  [{'OK' if ok else 'FAIL'}] {name}: {actual}")
        return ok

    def search(self, step, user, query, project=False):
        r = search(user, query, project)
        self.searches.append({"step": step, **{k: v for k, v in r.items() if not k.startswith("_")}})
        self.log(f"  · {r['user']} «{query}» → {r['status']} за {r['seconds']} с, листи {r['ids']}, "
                 f"відкинуто {r['dropped']}")
        return r

    def sync(self, step):
        r = wait_sync()
        self.syncs.append({"step": step, **r})
        detail = (f"seq {r['target_seq']} за {r['seconds']} с, docs {r.get('docs')}, chunks {r.get('chunks')}"
                  if r["ok"] else r)
        self.check(step, "RAG застосував журнал і статуси", r["ok"], detail)
        return r["ok"]

    def step(self, step, name):
        note, events = apply_step(name, added_key=self.added_key)
        self.log(f"\n=== {step}. {name}: {note} (подій {events})")
        if name in ACCOUNT_STEPS:
            self.check(step, "зміна скриньки одразу дала події стрічки", events > 0, events)
        return self.sync(step)

    # ── прогін ───────────────────────────────────────────────────────────────
    def run(self):
        from email_assistant.models import EmailAccount
        from rag_assistant.knowledge import instance_id
        from rag_assistant.models import KnowledgeDoc, KnowledgeSettings, RetrievalJob
        anna, boris = users()
        started = timezone.now()
        self.log(f"=== прогін {self.run_id}")
        fixes = restore_baseline()
        self.log(f"  вихідний стан: {fixes or 'без змін'}")
        rag_before = capabilities_index()
        sales = EmailAccount.objects.get(email_address=SALES).pk
        d = {k: doc_id(k) for k in BASE_KEYS}
        vertrieb = {d[k] for k in ("m1", "m2", "m3", "m4", "m5")}

        self.log("\n=== 1. вихідна індексація")
        self.sync(1)
        r = self.search(2, anna, "Liefertermin KW 46 zugesagt")
        self.check(2, "anna знаходить обіцянку KW 46 (m2)", d["m2"] in r["ids"], r["ids"])
        r = self.search(3, anna, "Kalibrierprotokoll SN-77821")
        self.check(3, "anna не бачить лист Бориса (m7)", d["m7"] not in r["ids"], r["ids"])
        r = self.search(3, boris, "Kalibrierprotokoll SN-77821")
        self.check(3, "boris знаходить m7", d["m7"] in r["ids"], r["ids"])
        self.check(3, "boris не бачить листів vertrieb", not set(r["ids"]) & vertrieb, r["ids"])
        r = self.search(4, anna, "Steckverbinder SMA IP67")
        att = [h for h in r["hits"] if h["document_id"] == d["m3"] and h["attachment"]]
        self.check(4, "фрагмент TXT-вкладення m3", bool(att), [(h["document_id"], h["attachment"]) for h in r["hits"][:3]])
        r = self.search(5, anna, "Angebot ANT-2400", project=True)
        self.check(5, "фільтр проєкту — лише листи гілки ANT-2400",
                   r["ids"] and set(r["ids"]) <= {d["m1"], d["m2"], d["m3"]}, r["ids"])

        self.step(6, "new-mail")
        added = doc_id(self.added_key)
        r = self.search(6, anna, "Expresszuschlag KW 45")
        self.check(6, "новий лист знайдено", added in r["ids"], r["ids"])
        r = self.search(6, anna, "Prüfbericht Vibrationstest")
        self.check(6, "вкладення нового листа знайдено",
                   any(h["document_id"] == added and h["attachment"] for h in r["hits"]), r["ids"])

        self.step(7, "edit")
        rev = KnowledgeDoc.objects.get(obj_id=message("m4").pk).revision
        r = self.search(7, anna, "Lieferung Kabel CAB-7 verschiebt sich")
        cab = [h for h in r["hits"] if h["document_id"] == d["m4"]]
        text = r["_texts"].get(d["m4"], "")
        self.check(7, "CAB-7: лише чинна ревізія з новою датою",
                   bool(cab) and all(h["document_revision"] == rev for h in cab) and CAB_NEW in text
                   and CAB_OLD not in text, {"revision": rev, "hit_revisions": [h["document_revision"] for h in cab]})

        self.step(8, "reassign")
        r = self.search(8, anna, "Liefertermin KW 46 zugesagt")
        self.check(8, "anna без скриньки — Minerva не шукає", r["status"] == RetrievalJob.UNAVAILABLE and not r["ids"],
                   r["message"])
        r = self.search(8, boris, "Liefertermin KW 46 zugesagt")
        self.check(8, "boris (новий власник) знаходить m2", d["m2"] in r["ids"], r["ids"])
        hits = raw_search(anna.pk, [sales], "Liefertermin KW 46 zugesagt")
        self.check(8, "RAG напряму: user anna, скринька vertrieb → 0", not hits, hits)

        self.step(9, "restore-owner")
        r = self.search(9, anna, "Liefertermin KW 46 zugesagt")
        self.check(9, "права повернулись anna", d["m2"] in r["ids"], r["ids"])
        hits = raw_search(boris.pk, [sales], "Liefertermin KW 46 zugesagt")
        self.check(9, "RAG напряму: user boris, скринька vertrieb → 0", not hits, hits)

        self.step(10, "deactivate")
        hits = raw_search(anna.pk, [sales], "Liefertermin KW 46 zugesagt")
        self.check(10, "вимкнена скринька: RAG напряму → 0", not hits, hits)
        self.step(10, "activate")
        r = self.search(10, anna, "Liefertermin KW 46 zugesagt")
        self.check(10, "скринька знову активна — m2 знайдено", d["m2"] in r["ids"], r["ids"])

        self.step(11, "exclude")
        r = self.search(11, anna, "Lieferung Kabel CAB-7 verschiebt sich")
        self.check(11, "приватна гілка CAB-7 зникла", d["m4"] not in r["ids"], r["ids"])
        self.step(11, "include")
        r = self.search(11, anna, "Lieferung Kabel CAB-7 verschiebt sich")
        self.check(11, "гілка CAB-7 повернулась", d["m4"] in r["ids"], r["ids"])

        self.step(12, "delete")
        r = self.search(12, anna, "Rabatt 8 % Zahlungsziel 30 Tage")
        self.check(12, "лист у кошику (m5) зник", d["m5"] not in r["ids"], r["ids"])
        self.step(12, "undelete")
        r = self.search(12, anna, "Rabatt 8 % Zahlungsziel 30 Tage")
        self.check(12, "лист із кошика повернувся", d["m5"] in r["ids"], r["ids"])

        self.step(13, "purge")
        r = self.search(13, anna, "Prüfbericht Vibrationstest")
        self.check(13, "видалений лист і вкладення зникли", added not in r["ids"], r["ids"])

        self.log("\n=== 14. Minerva без RAG (мертва адреса, потім повертається)")
        for name, ok, actual in fallback_checks(simulate=True):
            self.check(14, name, ok, actual)

        if self.outage:
            self._outage()

        self.log("\n=== кінець: повернення вихідного стану")
        fixes = restore_baseline()
        self.log(f"  {fixes}")
        self.sync(99)
        fed = sorted(f"email:{i}" for i in KnowledgeDoc.objects.filter(fed=True).values_list("obj_id", flat=True))
        self.check(99, "у стрічці лише вихідні листи", fed == sorted(d[k] for k in INDEXED_KEYS), fed)
        return self.report(started, rag_before, KnowledgeSettings.get(), instance_id())

    def _outage(self):
        """Крок 15: Minerva недоступна для RAG (зупинка сервера 8010) → зміна → запуск → RAG наздоганяє."""
        if os.name != "nt":
            self.check(15, "недоступність Minerva", False, "лише Windows (скрипти e2e)")
            return
        script = Path(settings.BASE_DIR) / "rag_assistant" / "e2e" / "start_minerva.ps1"
        ps = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
        self.log("\n=== 15. Minerva недоступна → зміна → запуск")
        subprocess.run(ps + ["Get-NetTCPConnection -State Listen -LocalPort 8010 -ErrorAction SilentlyContinue | "
                             "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -Confirm:$false }"],
                       capture_output=True)
        note, _ = apply_step("exclude")
        self.log(f"  сервер зупинено; {note}")
        time.sleep(20)
        idx = capabilities_index()
        self.check(15, "поки Minerva недоступна — курсор стоїть, last_error unavailable",
                   (idx.get("applied_seq") or 0) < last_seq() and bool(idx.get("last_error")),
                   {"applied_seq": idx.get("applied_seq"), "last_seq": last_seq(),
                    "last_error": (idx.get("last_error") or {}).get("code")})
        subprocess.run(ps + [f"& '{script}' -Background"], capture_output=True)
        self.sync(15)
        anna, _ = users()
        r = self.search(15, anna, "Lieferung Kabel CAB-7 verschiebt sich")
        self.check(15, "після наздоганяння CAB-7 приховано", doc_id("m4") not in r["ids"], r["ids"])
        apply_step("include")
        self.sync(15)

    def report(self, started, rag_before, ks, source):
        times = [s["seconds"] for s in self.searches if s["status"] == "completed"]
        failed = [c for c in self.checks if not c["ok"]]
        commit = subprocess.run(["git", "-C", str(settings.BASE_DIR), "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        return {
            "report_version": REPORT_VERSION, "run_id": self.run_id,
            "started_at": started.isoformat(), "finished_at": timezone.now().isoformat(),
            "duration_s": round(time.monotonic() - self.t0, 1),
            "minerva": {"source_instance_id": source, "generation": str(ks.generation), "commit": commit},
            "rag": {"index_before": rag_before, "index_after": capabilities_index()},
            "summary": {"checks": len(self.checks), "ok": len(self.checks) - len(failed), "failed": len(failed),
                        "dropped_total": sum(s["dropped"] for s in self.searches),
                        "search_seconds": {"count": len(times),
                                           "p50": round(statistics.median(times), 2) if times else None,
                                           "max": max(times) if times else None},
                        "sync_seconds_max": max((s["seconds"] for s in self.syncs), default=None)},
            "failed": failed, "checks": self.checks, "searches": self.searches, "syncs": self.syncs,
        }
