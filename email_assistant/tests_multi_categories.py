"""Лист у кількох своїх категоріях (DigiKey + митний брокер…): основна, додати / лише ця / прибрати, «↺ За правилами».
python manage.py test email_assistant --settings=tabele.settings_test"""
import datetime
import importlib

from django.apps import apps

from email_assistant import work_categories
from email_assistant.models import EmailMessage, EmailThread, MailCategory
from email_assistant.tests_work_categories import ME, Base

ORDER = "99823894"


class MultiBase(Base):
    """DigiKey — за замовленням джерела digikey, «Митниця» — за відправником dakosy (ABD митного брокера)."""

    def setUp(self):
        super().setUp()
        from sales.models import SalesOrder, SalesSource
        src = SalesSource.objects.create(slug="digikey", name="DigiKey")
        SalesOrder.objects.create(source="digikey", order_number=ORDER, order_date=datetime.date(2026, 10, 1))
        self.dk.sources.add(src)
        self.customs = MailCategory.objects.create(name="Митниця", icon="🛃", senders="dakosy", order=2)

    def abd(self, **kw):
        m = self.mail("noreply@dakosy.de", subject=f"ABD Ausfuhr {ORDER}", body=f"Auftrag {ORDER} — ABD anbei",
                      **kw)
        m.refresh_from_db()
        return m

    def cats(self, m):
        """Категорії листа з бази: [основна, інші за порядком]."""
        return [c.name for c in work_categories.current(m)]


class MultiCategoryTests(MultiBase):
    def test_abd_of_digikey_order_is_in_both(self):
        m = self.abd()
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])
        self.assertEqual(m.work_category, self.dk)   # основна — перша за порядком (за нею час askDANTE)
        primary, every = work_categories.resolve_all(m)
        self.assertEqual((primary, every), (self.dk, [self.dk, self.customs]))
        counts = {c.name: n for c, n, _u in work_categories.counts(EmailMessage.objects.all())}
        self.assertEqual((counts["DigiKey"], counts["Митниця"]), (1, 1))

    def test_reply_in_thread_inherits_every_category(self):
        first = self.abd(msg_id="<abd1@x>")
        reply = self.mail(ME, subject="Re: Danke", to=["kollegin@sevskiy.example"], reply_to="<abd1@x>",
                          folder="Sent")
        reply.refresh_from_db()
        self.assertEqual(reply.thread_id, first.thread_id)
        self.assertEqual(self.cats(reply), ["DigiKey", "Митниця"])

    def test_add_keeps_other_categories(self):
        m = self.mail("info@forwarder.example", subject=f"Sendung zu {ORDER}")
        m.refresh_from_db()
        self.assertEqual(self.cats(m), ["DigiKey"])
        r = work_categories.set_for_message(m, self.customs, "domain")
        self.assertEqual((r["mode"], r["key"]), ("add", "@forwarder.example"))
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])
        self.customs.refresh_from_db()
        self.assertIn("@forwarder.example", self.customs.lines("senders"))
        # той самий відправник в іншій гілці — теж в обох? Ні: лише «Митниця» (замовлення немає)
        other = self.mail("info@forwarder.example", subject="Neue Preise")
        other.refresh_from_db()
        self.assertEqual(self.cats(other), ["Митниця"])

    def test_only_this_one_replaces_and_pins_thread(self):
        self.dk.senders += "\nnoreply@dakosy.de"
        self.dk.save()
        m = self.abd()
        r = work_categories.set_for_message(m, self.customs, "sender", "only")
        self.assertEqual(self.cats(m), ["Митниця"])
        self.dk.refresh_from_db()
        self.assertNotIn("noreply@dakosy.de", self.dk.lines("senders"))   # адреса — лише в цій категорії
        t = EmailThread.objects.get(pk=m.thread_id)
        self.assertEqual((t.work_category, t.work_categories_only, list(t.work_categories.all())),
                         (self.customs, True, [self.customs]))
        self.assertEqual([c["label"] for c in r["categories"]], ["🛃 Митниця"])
        # нова відповідь у гілці з номером замовлення — правило DigiKey гілці вже нічого не додає
        reply = self.mail(ME, subject=f"Re: ABD {ORDER}", to=["noreply@dakosy.de"], reply_to=m.message_id,
                          folder="Sent")
        reply.refresh_from_db()
        self.assertEqual(self.cats(reply), ["Митниця"])

    def test_remove_rule_line_or_pin_when_another_rule_holds(self):
        m = self.abd()
        # «Митниця» тримає лише правило адреси / домену — прибираємо правило, гілку не закріплюємо
        self.customs.senders = "@dakosy.de"
        self.customs.save()
        r = work_categories.set_for_message(m, self.customs, "domain", "remove")
        self.assertEqual((self.cats(m), r["pinned"]), (["DigiKey"], False))
        self.customs.refresh_from_db()
        self.assertEqual(self.customs.lines("senders"), [])
        self.assertFalse(EmailThread.objects.get(pk=m.thread_id).work_categories_only)
        # DigiKey тримає номер замовлення — правила адреси немає, тож гілка лишається лише в інших
        self.customs.senders = "dakosy"
        self.customs.save()
        work_categories.recompute(EmailMessage.objects.filter(pk=m.pk))
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])
        r = work_categories.set_for_message(m, self.dk, "sender", "remove")
        self.assertTrue(r["pinned"])
        self.assertEqual(self.cats(m), ["Митниця"])
        t = EmailThread.objects.get(pk=m.thread_id)
        self.assertEqual((t.work_categories_only, list(t.work_categories.all())), (True, [self.customs]))
        why = work_categories.explain(EmailMessage.objects.get(pk=m.pk))
        self.assertTrue(why["manual"]["only"])
        # «↺ За правилами» — знову обидві
        r = work_categories.reset_thread(m)
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])
        t.refresh_from_db()
        self.assertEqual((t.work_category, t.work_categories_only, t.work_categories.count()), (None, False, 0))

    def test_without_category_clears_and_pins_empty(self):
        m = self.abd()
        work_categories.set_for_message(m, None, "sender")
        self.assertEqual(self.cats(m), [])   # замовлення DigiKey далі в листі — гілку залишено без категорії
        m.refresh_from_db()
        self.assertIsNone(m.work_category)
        self.assertIn("без своєї категорії", work_categories.explain(m)["text"])
        work_categories.reset_thread(m)
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])

    def test_primary_by_hand(self):
        m = self.abd()
        r = work_categories.set_primary(m, self.customs)
        self.assertEqual(self.cats(m), ["Митниця", "DigiKey"])   # основна першою
        self.assertTrue(r["categories"][0]["primary"])
        m.refresh_from_db()
        self.assertEqual(m.work_category, self.customs)
        why = work_categories.explain(m)
        self.assertEqual([(it["cat"].name, it["primary"], it["manual"]) for it in why["categories"]],
                         [("Митниця", True, True), ("DigiKey", False, False)])
        self.assertIn("основна для гілки", why["categories"][0]["reasons"][0])
        self.assertTrue(any("замовлення" in t for t in why["categories"][1]["reasons"]))
        self.assertFalse(why["stale"])

    def test_explain_lists_each_category_with_its_rule(self):
        m = self.abd()
        why = work_categories.explain(m)
        self.assertEqual([(it["cat"].name, it["how"], it["has"]) for it in why["categories"]],
                         [("DigiKey", "order", True), ("Митниця", "sender", True)])
        self.assertEqual(why["categories"][1]["rule"], "dakosy")
        self.assertEqual((why["how"], why["also"]), ("order", [self.customs]))

    def test_recompute_adds_second_category_to_old_mail(self):
        self.customs.is_active = False
        self.customs.save()
        m = self.abd()
        self.assertEqual(self.cats(m), ["DigiKey"])
        self.customs.is_active = True
        self.customs.save()
        self.assertTrue(work_categories.explain(m)["stale"])
        self.assertEqual(work_categories.recompute(EmailMessage.objects.all()), 1)
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])
        self.assertEqual(work_categories.recompute(EmailMessage.objects.all()), 0)   # нічого нового


class InboxMultiTests(MultiBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def post(self, m, **body):
        import json
        return self.client.post(f"/email/message/{m.pk}/work-category/", json.dumps(body),
                                content_type="application/json").json()

    def test_filter_tags_and_counts(self):
        m = self.abd()
        self.mail("x@other.example", subject="Andere Mail")
        for cat in (self.dk, self.customs):
            page = self.client.get("/email/", {"wcat": cat.pk})
            self.assertContains(page, "ABD Ausfuhr")
            self.assertNotContains(page, "Andere Mail")
        page = self.client.get("/email/", {"wcat": 0})
        self.assertContains(page, "Andere Mail")
        self.assertNotContains(page, "ABD Ausfuhr")
        page = self.client.get("/email/")
        self.assertContains(page, f'data-wcats="{self.dk.pk},{self.customs.pk}"')
        self.assertContains(page, "Основна своя категорія")
        self.assertContains(page, 'id="em-ctx-wcat-only"')
        self.assertEqual(self.cats(m), ["DigiKey", "Митниця"])

    def test_menu_modes(self):
        m = self.mail("info@forwarder.example", subject=f"Sendung {ORDER}")
        d = self.post(m, category=self.customs.pk, scope="thread")
        self.assertEqual(([c["label"] for c in d["categories"]], d["mode"]), (["🟥 DigiKey", "🛃 Митниця"], "add"))
        d = self.post(m, category=self.customs.pk, scope="thread", mode="primary")
        self.assertEqual(d["categories"][0]["label"], "🛃 Митниця")
        d = self.post(m, category=self.customs.pk, scope="thread", mode="remove")
        self.assertEqual([c["label"] for c in d["categories"]], ["🟥 DigiKey"])
        d = self.post(m, category=self.customs.pk, scope="thread", mode="only")
        self.assertEqual([c["label"] for c in d["categories"]], ["🛃 Митниця"])
        d = self.post(m, category="", mode="rules")
        self.assertEqual([c["label"] for c in d["categories"]], ["🟥 DigiKey"])
        self.assertFalse(self.post(m, category="", mode="primary")["ok"])

    def test_why_view_lists_categories_and_manual_state(self):
        m = self.abd()
        self.post(m, category=self.dk.pk, scope="thread", mode="remove")
        d = self.client.get(f"/email/message/{m.pk}/why/").json()
        self.assertEqual([c["label"] for c in d["mail_categories"]], ["🛃 Митниця"])
        self.assertEqual((d["manual"]["only"], [c["label"] for c in d["manual"]["categories"]]),
                         (True, ["🛃 Митниця"]))


class MigrationTests(MultiBase):
    def test_old_category_becomes_primary_and_thread_choice_stays(self):
        m = self.abd()
        through = EmailMessage.work_categories.through
        through.objects.all().delete()   # як до міграції: лише основна категорія
        EmailThread.work_categories.through.objects.all().delete()
        EmailThread.objects.filter(pk=m.thread_id).update(work_category=self.customs)
        mig = importlib.import_module("email_assistant.migrations.0034_multi_work_categories")
        mig.forwards(apps, None)
        self.assertEqual(list(through.objects.filter(emailmessage_id=m.pk).values_list("mailcategory_id", flat=True)),
                         [self.dk.pk])
        t = EmailThread.objects.get(pk=m.thread_id)
        self.assertEqual((list(t.work_categories.all()), t.work_categories_only), ([self.customs], False))
        mig.forwards(apps, None)   # повтор — без дублів
        self.assertEqual(through.objects.filter(emailmessage_id=m.pk).count(), 1)
        work_categories.recompute(EmailMessage.objects.filter(pk=m.pk))
        self.assertEqual(self.cats(m), ["Митниця", "DigiKey"])   # вручну для гілки — основна, правила додають інші
