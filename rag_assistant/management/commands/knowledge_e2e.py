"""
Наскрізний тест Minerva ↔ приватний RAG на синтетичних даних (rag_assistant/e2e/README.md).

Працює лише з --settings=tabele.settings_e2e: окрема PostgreSQL minerva_e2e, окремі media, жодних справжніх листів.

  seed                       користувачі, дві скриньки, клієнт, постачальник, проєкт, листи, токен для RAG у файл
  step <крок>                new-mail | edit | reassign | restore-owner | deactivate | activate |
                             exclude | include | delete | purge
  search "запит" [--user]    пошук через RAG від імені користувача (hits перевіряє Minerva)
  status                     стрічка, статуси індексації від RAG, запобіжник
  rag --url URL [--key-file | --clear-key]  адреса й ключ приватного RAG (ключ лише з файлу, не друкується)
  fallback [--simulate]      робота Minerva без RAG (--simulate — тимчасово мертва адреса замість зупинки RAG)
"""
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

DOMAIN = "minerva-e2e.test"
SALES = f"vertrieb@{DOMAIN}"
SERVICE = f"service@{DOMAIN}"
CUSTOMER_EMAIL = "einkauf@muster-antennen.test"
SUPPLIER_EMAIL = "info@beispiel-kabel.test"
DEAD_URL = "http://127.0.0.1:9"
LOCAL_RAG = "http://127.0.0.1:8788"
STEPS = ("new-mail", "edit", "reassign", "restore-owner", "deactivate", "activate",
         "exclude", "include", "delete", "purge")
BASE = datetime(2026, 10, 12, 8, 0, tzinfo=dt_tz.utc)

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
     "Hallo,\ndie Lieferung der Kabel CAB-7 verschiebt sich auf den 14.11.2026. Wir bitten um Entschuldigung.",
     None, False),
    ("m5", SALES, "INBOX", f"kollege@{DOMAIN}", [SALES], "Intern: Rabatt Muster Antennen",
     "Für Muster Antennen gilt maximal 8 % Rabatt, Zahlungsziel 30 Tage.", None, False),
    ("m6", SALES, "INBOX", "news@shop-e2e.test", [SALES], "Wochenangebote",
     "Diese Woche: Rabatte auf Steckverbinder.", None, True),
    ("m7", SERVICE, "INBOX", CUSTOMER_EMAIL, [SERVICE], "Kalibrierprotokoll fehlt",
     "Für das Messgerät mit Seriennummer SN-77821 fehlt das Kalibrierprotokoll.", None, False),
]
NEW_MAIL = ("m8", SALES, "INBOX", CUSTOMER_EMAIL, [SALES], "Re: Anfrage Antenne ANT-2400, 500 Stück",
            "Können Sie die Lieferung auf KW 45 vorziehen? Einen Expresszuschlag würden wir akzeptieren.",
            None, False)


def _mid(key):
    return f"<{key}@{DOMAIN}>"


class Command(BaseCommand):
    help = "Синтетичне середовище для наскрізного тесту бази знань з приватним RAG (лише settings_e2e)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["seed", "step", "search", "status", "rag", "fallback"])
        parser.add_argument("arg", nargs="?", default="", help="крок для step / запит для search")
        parser.add_argument("--user", default="e2e_anna")
        parser.add_argument("--project", action="store_true", help="search: лише листи проєкту ANT-2400")
        parser.add_argument("--url", default="")
        parser.add_argument("--key-file", default="")
        parser.add_argument("--clear-key", action="store_true")
        parser.add_argument("--simulate", action="store_true")
        parser.add_argument("--rotate-token", action="store_true")

    def handle(self, *args, **opts):
        db = connection.settings_dict
        if not getattr(settings, "MINERVA_E2E", False) or not str(db.get("NAME", "")).startswith("minerva_e2e"):
            raise CommandError("Лише для ізольованого середовища: --settings=tabele.settings_e2e (БД minerva_e2e*).")
        getattr(self, "do_" + opts["action"])(opts)

    # ── seed ─────────────────────────────────────────────────────────────────
    def do_seed(self, opts):
        from api.models import APIKey
        from config.models import SystemSettings
        from crm.models import Customer
        from email_assistant.models import EmailAccount, EmailSettings
        from inventory.models import Supplier
        from rag_assistant.models import KnowledgeSettings, RagSettings

        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        admin = User.objects.filter(username="e2e_admin").first()
        if admin is None:
            admin = User.objects.create_superuser("e2e_admin", f"admin@{DOMAIN}", None)   # пароль не задано
        anna = self._user("e2e_anna")
        boris = self._user("e2e_boris")
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

        created = [self._mail(spec) for spec in MAILS]
        self._project(anna)
        from rag_assistant import knowledge
        result = knowledge.refresh_all()

        key = APIKey.objects.filter(name="rag-e2e").first()
        if key is None or opts["rotate_token"]:
            if key is not None:
                key.delete()
            key = APIKey.objects.create(name="rag-e2e", scopes=["knowledge:read", "knowledge:write"])
        token_file = self._write_secret("minerva-knowledge-token.txt", key.key)

        self.stdout.write(self.style.SUCCESS(
            f"Готово: нових листів {sum(1 for c in created if c)}, подій стрічки {result['events']}."))
        self.stdout.write(f"source_instance_id: {settings.MINERVA_INSTANCE_ID}")
        self.stdout.write(f"generation:         {KnowledgeSettings.get().generation}")
        self.stdout.write(f"Токен knowledge:read/write (значення не друкується): {token_file}")
        self.stdout.write(f"Користувачі: e2e_anna (id {anna.pk}, {SALES}), e2e_boris (id {boris.pk}, {SERVICE}); "
                          f"e2e_admin без пароля.")

    def _user(self, username):
        user = User.objects.filter(username=username).first()
        if user is None:
            user = User.objects.create_user(username, f"{username}@{DOMAIN}", None, is_staff=True)
        return user

    def _mail(self, spec):
        from email_assistant.models import EmailAccount, EmailMessage
        from email_assistant.sync import save_message
        key, mailbox, folder, sender, to, subject, body, attachment, newsletter = spec
        if EmailMessage.objects.filter(message_id=_mid(key)).exists():
            return None
        account = EmailAccount.objects.get(email_address=mailbox)
        n = int(key[1:])
        attachments = []
        if attachment:
            name, content = attachment
            rel = f"email_attachments/e2e/{key}/{name}"
            path = Path(settings.MEDIA_ROOT) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            attachments = [{"name": name, "content_type": "text/plain", "size": path.stat().st_size,
                            "file_path": rel}]
        refs = [_mid("m1")] if subject.startswith("Re: Anfrage") else []
        return save_message(account, {
            "uid": 1000 + n, "message_id": _mid(key), "in_reply_to": refs[0] if refs else "",
            "references": refs, "subject": subject, "from_email": sender, "from_name": "", "to_emails": to,
            "cc_emails": [], "body_text": body, "body_html": "", "attachments": attachments, "is_read": False,
            "list_unsubscribe": newsletter, "sent_at": BASE + timedelta(hours=n)}, folder)

    def _project(self, owner):
        from email_assistant.models import EmailMessage
        from projects import services
        from projects.models import Project
        if Project.objects.filter(name="Antennen Muster 2026").exists():
            return
        thread = EmailMessage.objects.get(message_id=_mid("m1")).thread
        services.create_project(name="Antennen Muster 2026", user=owner, threads=[thread])

    def _write_secret(self, name, value):
        folder = Path(settings.E2E_HOME) / "secrets"
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":   # лише поточний користувач і SYSTEM
            who = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
            subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"{who}:(OI)(CI)F",
                            "*S-1-5-18:(OI)(CI)F"], check=True, capture_output=True)
        else:
            folder.chmod(0o700)
        path = folder / name
        path.write_text(value, encoding="ascii")
        if os.name != "nt":
            path.chmod(0o600)
        return path

    # ── step ─────────────────────────────────────────────────────────────────
    def do_step(self, opts):
        from email_assistant.models import EmailAccount, EmailMessage, EmailThread
        from rag_assistant.models import KnowledgeChange
        step = opts["arg"]
        if step not in STEPS:
            raise CommandError(f"Крок: {', '.join(STEPS)}")
        sales = EmailAccount.objects.get(email_address=SALES)
        before = KnowledgeChange.objects.order_by("-id").values_list("id", flat=True).first() or 0
        with transaction.atomic():
            if step == "new-mail":
                msg = self._mail(NEW_MAIL)
                note = f"лист {msg.pk if msg else '(уже є)'}: «Expresszuschlag», KW 45"
            elif step == "edit":
                msg = EmailMessage.objects.get(message_id=_mid("m4"))
                msg.body_text = msg.body_text.replace("14.11.2026", "21.11.2026")
                msg.save()
                note = f"лист {msg.pk}: нова дата CAB-7 — 21.11.2026"
            elif step in ("reassign", "restore-owner"):
                sales.user = User.objects.get(username="e2e_boris" if step == "reassign" else "e2e_anna")
                sales.save()
                note = f"власник {SALES} → {sales.user.username}"
            elif step in ("deactivate", "activate"):
                sales.is_active = step == "activate"
                sales.save()
                note = f"{SALES} активна: {sales.is_active}"
            elif step in ("exclude", "include"):
                thread = EmailThread.objects.get(pk=EmailMessage.objects.get(message_id=_mid("m4")).thread_id)
                thread.knowledge_excluded = step == "exclude"
                thread.save(update_fields=["knowledge_excluded"])
                note = f"гілка {thread.pk} (CAB-7) приватна: {thread.knowledge_excluded}"
            elif step == "delete":
                msg = EmailMessage.objects.get(message_id=_mid("m5"))
                msg.is_deleted = True
                msg.save(update_fields=["is_deleted"])
                note = f"лист {msg.pk} (Rabatt) у кошику"
            else:   # purge
                msg = EmailMessage.objects.filter(message_id=_mid("m3")).first()
                note = f"лист {msg.pk} (специфікація) видалено з БД" if msg else "лист m3 уже видалено"
                if msg:
                    msg.delete()
        if step in ("reassign", "restore-owner", "deactivate", "activate"):
            deadline = time.monotonic() + 10   # документи скриньки оновлює фоновий потік після commit
            while time.monotonic() < deadline and not KnowledgeChange.objects.filter(id__gt=before).exists():
                time.sleep(0.2)
            time.sleep(0.5)
        self.stdout.write(self.style.SUCCESS(f"{step}: {note}"))
        new = KnowledgeChange.objects.filter(id__gt=before).count()
        self.stdout.write(f"  нових подій стрічки: {new}")
        self._feed_tail(min(new, 10))

    # ── search ───────────────────────────────────────────────────────────────
    def do_search(self, opts):
        from projects.models import Project
        from rag_assistant import retrieval
        user = User.objects.get(username=opts["user"])
        project_id = Project.objects.get(name="Antennen Muster 2026").pk if opts["project"] else None
        started = time.monotonic()
        job = retrieval.start(user, opts["arg"], purpose="history", project_id=project_id,
                              wait_ms=retrieval.WAIT_EXTENDED_MS)
        while not job.is_terminal and time.monotonic() - started < 30:
            time.sleep(max(0.2, job.poll_after_ms / 1000))
            job = retrieval.poll(job)
        took = time.monotonic() - started
        self.stdout.write(f"[{opts['user']}] «{opts['arg']}» → {job.status} за {took:.1f} с: {job.message}")
        for h in job.hits:
            self.stdout.write(f"  {h['document_id']:<10} rev {h.get('document_revision')}  {h['subject'][:50]}  "
                              f"| {' '.join(h['text'].split())[:90]}")
        if job.dropped:
            self.stdout.write(f"  відкинуто Minerva: {job.dropped}")

    # ── status ───────────────────────────────────────────────────────────────
    def do_status(self, opts):
        from email_assistant.models import EmailMessage
        from rag_assistant import knowledge, retrieval
        from rag_assistant.models import KnowledgeChange, KnowledgeDoc, KnowledgeSettings
        s = KnowledgeSettings.get()
        last = KnowledgeChange.objects.order_by("-id").values_list("id", flat=True).first()
        self.stdout.write(f"source_instance_id {knowledge.instance_id()} · generation {s.generation} · "
                          f"останній seq {last or 0} · індексування {'увімк.' if s.enabled else 'пауза'}")
        self.stdout.write(f"RAG: {retrieval.base_url(s) or '—'} · ключ {'задано' if s.private_api_key else 'немає'}"
                          f" · запобіжник {retrieval.breaker_until(s) or 'вимкнено'} (збоїв {s.rag_failures})")
        subjects = dict(EmailMessage.objects.values_list("pk", "subject"))
        for d in KnowledgeDoc.objects.order_by("obj_id"):
            self.stdout.write(f"  email:{d.obj_id:<4} rev {d.revision:<2} {'у стрічці' if d.fed else 'tombstone':<9} "
                              f"статус RAG {d.state:<8} (rev {d.status_revision}, фрагм. {d.chunks}) "
                              f"{subjects.get(d.obj_id, '— видалено —')[:40]} {d.error[:60]}")

    def _feed_tail(self, n):
        from rag_assistant.models import KnowledgeChange
        for c in KnowledgeChange.objects.order_by("-id")[:n][::-1]:
            self.stdout.write(f"  seq {c.id}: {c.action} email:{c.obj_id} {c.reason}")

    # ── rag ──────────────────────────────────────────────────────────────────
    def do_rag(self, opts):
        from rag_assistant.models import KnowledgeSettings
        s = KnowledgeSettings.get()
        if opts["url"]:
            s.private_base_url = opts["url"].rstrip("/")
        if opts["key_file"]:
            key = Path(opts["key_file"]).read_text(encoding="utf-8").strip()
            if not key:
                raise CommandError("Файл ключа порожній.")
            s.private_api_key = key
        if opts["clear_key"]:
            s.private_api_key = ""
        s.rag_failures, s.rag_down_until = 0, None
        s.save()
        self.stdout.write(self.style.SUCCESS(
            f"RAG: {s.private_base_url or '—'} · ключ {'задано' if s.private_api_key else 'немає'} "
            f"(значення не показується)"))

    # ── fallback ─────────────────────────────────────────────────────────────
    def do_fallback(self, opts):
        """Minerva без RAG: пошук швидко каже «недоступний», запобіжник вмикається, пошта й стрічка працюють."""
        from django.test import Client

        from api.models import APIKey
        from email_assistant.models import EmailMessage
        from rag_assistant import retrieval
        from rag_assistant.models import KnowledgeSettings, RetrievalJob
        s = KnowledgeSettings.get()
        saved_url = s.private_base_url
        KnowledgeSettings.objects.filter(pk=1).update(
            rag_failures=0, rag_down_until=None, **({"private_base_url": DEAD_URL} if opts["simulate"] else {}))
        anna = User.objects.get(username="e2e_anna")
        ok = True
        try:
            for n in range(1, 5):
                t = time.monotonic()
                job = retrieval.start(anna, "Liefertermin ANT-2400", wait_ms=retrieval.WAIT_AUTO_MS)
                took = time.monotonic() - t
                good = job.status == RetrievalJob.UNAVAILABLE and took < 10
                ok &= good
                self.stdout.write(f"  пошук {n}: {job.status} за {took:.1f} с — {job.message} "
                                  f"{'OK' if good else 'FAIL'}")
            paused = retrieval.breaker_until() is not None
            ok &= paused
            self.stdout.write(f"  запобіжник після 3 збоїв: {'пауза' if paused else 'НЕ ввімкнувся'}")

            msg = EmailMessage.objects.get(message_id=_mid("m2"))
            client = Client()
            client.force_login(anna)
            history = client.get(f"/email/message/{msg.pk}/history/")
            rag = client.post(f"/email/message/{msg.pk}/rag-search/")
            token = APIKey.objects.get(name="rag-e2e").key
            feed = client.get("/api/v1/knowledge/changes/", {"after": 0, "limit": 1},
                              HTTP_AUTHORIZATION=f"Token {token}")
            checks = [("«📜 Історія» листа (лише Minerva)", history.status_code == 200 and "events" in history.json()),
                      ("«🔎 Доповнити через RAG» → чесне «недоступний»",
                       rag.status_code == 200 and rag.json().get("status") == RetrievalJob.UNAVAILABLE),
                      ("стрічка /api/v1/knowledge/changes/", feed.status_code == 200)]
            for label, good in checks:
                ok &= good
                self.stdout.write(f"  {label}: {'OK' if good else 'FAIL'}")
        finally:
            KnowledgeSettings.objects.filter(pk=1).update(
                rag_failures=0, rag_down_until=None, **({"private_base_url": saved_url} if opts["simulate"] else {}))
        self.stdout.write(self.style.SUCCESS("Без RAG Minerva працює.") if ok else self.style.ERROR("Є збої — див. вище."))
