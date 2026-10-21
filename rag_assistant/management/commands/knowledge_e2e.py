"""
Наскрізний тест Minerva ↔ приватний RAG на синтетичних даних (rag_assistant/e2e/README.md).

Працює лише з --settings=tabele.settings_e2e: окрема PostgreSQL minerva_e2e, окремі media, жодних справжніх листів.

  seed                       користувачі, дві скриньки, клієнт, постачальник, проєкт, листи, токен для RAG у файл
  scenario [--outage]        повний повторюваний прогін із JSON-звітом (--outage — ще й недоступність Minerva)
  baseline                   повернути вихідний стан синтетичних даних
  step <крок>                new-mail | edit | reassign | restore-owner | deactivate | activate |
                             exclude | include | delete | undelete | purge
  search "запит" [--user]    пошук через RAG від імені користувача (hits перевіряє Minerva)
  status                     стрічка, статуси індексації від RAG, запобіжник
  rag --url URL [--key-file | --clear-key]  адреса й ключ приватного RAG (ключ лише з файлу, не друкується)
  fallback [--simulate]      робота Minerva без RAG (--simulate — тимчасово мертва адреса замість зупинки RAG)
"""
import json
import os
import subprocess
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from rag_assistant import e2e_scenario as sc


class Command(BaseCommand):
    help = "Синтетичне середовище для наскрізного тесту бази знань з приватним RAG (лише settings_e2e)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["seed", "scenario", "baseline", "step", "search", "status", "rag",
                                               "fallback"])
        parser.add_argument("arg", nargs="?", default="", help="крок для step / запит для search")
        parser.add_argument("--user", default="e2e_anna")
        parser.add_argument("--project", action="store_true", help="search: лише листи проєкту ANT-2400")
        parser.add_argument("--url", default="")
        parser.add_argument("--key-file", default="")
        parser.add_argument("--clear-key", action="store_true")
        parser.add_argument("--simulate", action="store_true")
        parser.add_argument("--rotate-token", action="store_true")
        parser.add_argument("--outage", action="store_true", help="scenario: крок 15 із зупинкою сервера 8010")
        parser.add_argument("--report", default="", help="scenario: шлях JSON-звіту")

    def handle(self, *args, **opts):
        db = connection.settings_dict
        if not getattr(settings, "MINERVA_E2E", False) or not str(db.get("NAME", "")).startswith("minerva_e2e"):
            raise CommandError("Лише для ізольованого середовища: --settings=tabele.settings_e2e (БД minerva_e2e*).")
        getattr(self, "do_" + opts["action"])(opts)

    # ── seed ─────────────────────────────────────────────────────────────────
    def do_seed(self, opts):
        from api.models import APIKey
        from rag_assistant.models import KnowledgeSettings
        result = sc.seed()
        key = APIKey.objects.filter(name="rag-e2e").first()
        if key is None or opts["rotate_token"]:
            if key is not None:
                key.delete()
            key = APIKey.objects.create(name="rag-e2e", scopes=["knowledge:read", "knowledge:write"])
        token_file = self._write_secret("minerva-knowledge-token.txt", key.key)
        anna, boris = result["anna"], result["boris"]
        self.stdout.write(self.style.SUCCESS(f"Готово: нових листів {result['created']}, подій стрічки {result['events']}."))
        self.stdout.write(f"source_instance_id: {settings.MINERVA_INSTANCE_ID}")
        self.stdout.write(f"generation:         {KnowledgeSettings.get().generation}")
        self.stdout.write(f"Токен knowledge:read/write (значення не друкується): {token_file}")
        self.stdout.write(f"Користувачі: e2e_anna (id {anna.pk}, {sc.SALES}), e2e_boris (id {boris.pk}, {sc.SERVICE}); "
                          f"e2e_admin без пароля.")

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

    # ── scenario / baseline / step ───────────────────────────────────────────
    def do_scenario(self, opts):
        scenario = sc.Scenario(log=self.stdout.write, outage=opts["outage"])
        report = scenario.run()
        path = Path(opts["report"] or Path(settings.E2E_HOME) / "reports" / f"e2e-{report['run_id']}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        s = report["summary"]
        self.stdout.write(f"\nПеревірок {s['checks']}, OK {s['ok']}, FAIL {s['failed']}; пошук p50 "
                          f"{s['search_seconds']['p50']} с, max {s['search_seconds']['max']} с; звіт: {path}")
        if s["failed"]:
            raise CommandError(f"Є збої: {[c['name'] for c in report['failed']]}")

    def do_baseline(self, opts):
        fixes = sc.restore_baseline()
        self.stdout.write(self.style.SUCCESS(f"Вихідний стан: {fixes or 'без змін'}"))
        self._feed_tail(5)

    def do_step(self, opts):
        try:
            note, events = sc.apply_step(opts["arg"])
        except ValueError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"{opts['arg']}: {note}"))
        self.stdout.write(f"  нових подій стрічки: {events}")
        self._feed_tail(min(events, 10))

    # ── search / status ──────────────────────────────────────────────────────
    def do_search(self, opts):
        r = sc.search(User.objects.get(username=opts["user"]), opts["arg"], project=opts["project"])
        self.stdout.write(f"[{r['user']}] «{r['query']}» → {r['status']} за {r['seconds']} с: {r['message']}")
        for h in r["hits"]:
            text = " ".join(r["_texts"].get(h["document_id"], "").split())[:80]
            self.stdout.write(f"  {h['document_id']:<10} rev {h['document_revision']}  score {h['score']}  "
                              f"{h['attachment'] or ''} | {text}")
        if r["dropped"]:
            self.stdout.write(f"  відкинуто Minerva: {r['dropped']}")

    def do_status(self, opts):
        from email_assistant.models import EmailMessage
        from rag_assistant import knowledge, retrieval
        from rag_assistant.models import KnowledgeDoc, KnowledgeSettings
        s = KnowledgeSettings.get()
        self.stdout.write(f"source_instance_id {knowledge.instance_id()} · generation {s.generation} · "
                          f"останній seq {sc.last_seq()} · індексування {'увімк.' if s.enabled else 'пауза'}")
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

    # ── rag / fallback ───────────────────────────────────────────────────────
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

    def do_fallback(self, opts):
        """Minerva без RAG: пошук швидко каже «недоступний», запобіжник вмикається, пошта й стрічка працюють."""
        results = sc.fallback_checks(simulate=opts["simulate"])
        for name, ok, actual in results:
            self.stdout.write(f"  {name}: {'OK' if ok else 'FAIL'} — {actual}")
        ok = all(r[1] for r in results)
        self.stdout.write(self.style.SUCCESS("Без RAG Minerva працює.") if ok else self.style.ERROR("Є збої — див. вище."))
