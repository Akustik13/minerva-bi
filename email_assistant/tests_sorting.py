"""🧠 Розбір пошти (sorting.py): python manage.py test email_assistant --settings=tabele.settings_test (AI підмінено)"""
import datetime
import json
from unittest.mock import patch

from email_assistant import sorting
from email_assistant.models import EmailMessage, MailCategory, MailSortDecision, MailSortRun
from email_assistant.tests_work_categories import Base

D1, D2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 31)


class SyncThread:
    """Розбір без фону — у тому самому потоці й транзакції тесту."""
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class SortBase(Base):
    def setUp(self):
        super().setUp()
        from sales.models import SalesOrder, SalesSource
        SalesSource.objects.create(slug="ebay-de", name="eBay")
        self.orders = []
        for i in range(1, 6):
            SalesOrder.objects.create(source="ebay-de", order_number=f"EB-40211-00{i}",
                                      order_date=datetime.date(2026, 10, 1))
            self.orders.append(self.mail(f"buyer{i}@gmail.com", subject=f"Frage {i}",
                                         body=f"zu Auftrag EB-40211-00{i} bitte"))
        self.pcb = [self.mail("anna@pcbfab.example", subject=f"PCB quote {i}") for i in range(1, 4)]
        self.invoice = self.mail("billing@pcbfab.example", subject="Invoice 77")

    def ai_answer(self, items):
        return patch("email_assistant.ai_helper._call_ai_direct", return_value=json.dumps(items))

    def run_of(self, use_ai=False):
        cards, n = sorting.build(self.user, D1, D2, use_ai=use_ai)
        return MailSortRun.objects.create(user=self.user, date_from=D1, date_to=D2, status=MailSortRun.DONE,
                                          groups=cards, mails=n)


class BuildTests(SortBase):
    def test_rule_group_from_order_numbers_without_ai(self):
        with patch("email_assistant.ai_helper._call_ai_direct") as ai:
            cards, n = sorting.build(self.user, D1, D2, use_ai=False)
        ai.assert_not_called()
        self.assertEqual(n, 9)
        card = next(c for c in cards if c["source"] == "rules")
        self.assertEqual(card["sources"], ["ebay-de"])
        self.assertEqual(sorted(card["ids"]), sorted(m.pk for m in self.orders))
        self.assertIn("ebay-de", card["reason"])

    def test_ai_rule_is_checked_against_the_mails(self):
        bogus = 999999
        with self.ai_answer([
            {"name": "PCB-Angebote", "reason": "запити цін на плати", "ids": [m.pk for m in self.pcb] + [bogus],
             "senders": ["@pcbfab.example", "anna@pcbfab.example"], "subject_words": ["PCB", "Lieferung"],
             "existing": "DigiKey"},
            {"name": "Один лист", "ids": [self.invoice.pk]},
        ]):
            cards, _ = sorting.build(self.user, D1, D2, use_ai=True)
        ai_cards = [c for c in cards if c["source"] == "ai"]
        self.assertEqual(len(ai_cards), 1)   # група з одного листа — ні
        card = ai_cards[0]
        self.assertEqual(sorted(card["ids"]), sorted(m.pk for m in self.pcb))        # вигаданий id відкинуто
        self.assertEqual(card["senders"], ["anna@pcbfab.example"])   # @домен ловить і Invoice поза групою
        self.assertEqual(card["words"], ["PCB"])                     # «Lieferung» немає в темах
        self.assertEqual(card["existing"], "DigiKey")
        self.assertEqual(cards[0]["source"], "rules")                # правила — першими

    def test_ai_mails_exclude_rule_groups(self):
        with self.ai_answer([]) as ai:
            sorting.build(self.user, D1, D2, use_ai=True)
        prompt = ai.call_args[0][0]
        self.assertIn("PCB quote 1", prompt)
        self.assertNotIn("Frage 1", prompt)   # уже в групі за правилами

    def test_broken_ai_answer_gives_only_rule_groups(self):
        with patch("email_assistant.ai_helper._call_ai_direct", return_value="Sorry, no JSON"):
            cards, _ = sorting.build(self.user, D1, D2, use_ai=True)
        self.assertEqual([c["source"] for c in cards], ["rules"])


class DecisionTests(SortBase):
    def test_accept_creates_category_with_rules_and_new_mail_follows(self):
        run = self.run_of()
        card = run.groups[0]
        cat, changed = sorting.accept(self.user, run, card["key"], "eBay Kunden", [], [], card["ids"])
        self.assertEqual(cat.name, "eBay Kunden")
        self.assertEqual(list(cat.sources.values_list("slug", flat=True)), ["ebay-de"])
        self.assertEqual(changed, 5)
        self.assertEqual(EmailMessage.objects.filter(work_category=cat).count(), 5)
        from sales.models import SalesOrder
        SalesOrder.objects.create(source="ebay-de", order_number="EB-40211-009", order_date=D1)
        new = self.mail("buyer9@gmail.com", subject="Wo bleibt", body="Auftrag EB-40211-009")
        new.refresh_from_db()
        self.assertEqual(new.work_category, cat)
        run.refresh_from_db()
        self.assertFalse(any(g["key"] == card["key"] for g in run.groups))
        d = MailSortDecision.objects.get()
        self.assertEqual((d.decision, d.category, d.source), (MailSortDecision.ACCEPTED, cat, "rules"))

    def test_accept_ai_group_into_existing_category_with_unchecked_mail(self):
        with self.ai_answer([{"name": "PCB", "ids": [m.pk for m in self.pcb], "senders": ["anna@pcbfab.example"],
                              "subject_words": ["PCB"]}]):
            run = self.run_of(use_ai=True)
        card = next(g for g in run.groups if g["source"] == "ai")
        keep = [m.pk for m in self.pcb[:2]] + [self.invoice.pk]   # Invoice — не з картки, відкидається
        cat, _ = sorting.accept(self.user, run, card["key"], "", ["anna@pcbfab.example"], ["PCB"], keep, self.dk)
        self.assertEqual(cat, self.dk)
        self.assertIn("anna@pcbfab.example", cat.lines("senders"))
        self.assertIn("pcb", cat.lines("subject_words"))
        self.assertEqual(MailCategory.objects.count(), 2)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.work_category)

    def test_rejected_group_is_not_proposed_again(self):
        run = self.run_of()
        key = run.groups[0]["key"]
        sorting.reject(self.user, run, key)
        cards, _ = sorting.build(self.user, D1, D2, use_ai=False)
        self.assertFalse(any(c["key"] == key for c in cards))
        self.assertTrue(MailSortDecision.objects.filter(key=key, decision=MailSortDecision.REJECTED).exists())

    def test_ignore_time_marks_threads(self):
        from worktime.models import MailTimeIgnore
        run = self.run_of()
        card = run.groups[0]
        sorting.ignore_time(self.user, run, card["key"], card["ids"] + [self.invoice.pk])
        self.assertEqual(MailTimeIgnore.objects.filter(user=self.user, kind=MailTimeIgnore.THREAD).count(), 5)

    def test_wrong_key_is_refused(self):
        run = self.run_of()
        with self.assertRaises(ValueError):
            sorting.accept(self.user, run, "rules:nope", "X", [], [], [])


class PageTests(SortBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_start_shows_cards_and_actions(self):
        self.assertContains(self.client.get("/email/sort/"), "Розібрати")
        with patch.object(sorting.threading, "Thread", SyncThread):
            r = self.client.post("/email/sort/", {"action": "start", "from": "2026-10-01", "to": "2026-10-31"})
        run = MailSortRun.objects.get()
        self.assertRedirects(r, f"/email/sort/?run={run.pk}", fetch_redirect_response=False)
        self.assertEqual(run.status, MailSortRun.DONE)
        self.assertFalse(run.use_ai)   # ключа Anthropic немає — лише правила
        page = self.client.get(f"/email/sort/?run={run.pk}")
        self.assertContains(page, "Frage 3")   # приклад листа групи
        key = run.groups[0]["key"]
        r = self.client.post("/email/sort/", {"action": "accept", "run": run.pk, "key": key, "name": "eBay",
                                              "existing": "", "mail": run.groups[0]["ids"]})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(MailCategory.objects.filter(name="eBay").exists())

    def test_other_users_run_is_404(self):
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user("bob", "bob@x.example", "x", is_staff=True)
        run = MailSortRun.objects.create(user=other, date_from=D1, date_to=D2)
        r = self.client.post("/email/sort/", {"action": "reject", "run": run.pk, "key": "x"})
        self.assertEqual(r.status_code, 404)

    def test_links(self):
        self.assertContains(self.client.get("/email/"), "/email/sort/")
        self.assertContains(self.client.get("/admin/email_assistant/mailcategory/"), "/email/sort/")
