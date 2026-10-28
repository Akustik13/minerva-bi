"""Правий клік на своїй категорії в скриньці (views_categories.py):
python manage.py test email_assistant --settings=tabele.settings_test"""
import json
from unittest import mock

from django.contrib.auth import get_user_model

from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, MailCategory
from email_assistant.tests_work_categories import ME as ME_SENT, Base


class MenuBase(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.a = self.mail("orders@digikey.com", subject="Order 1")
        self.b = self.mail("orders@digikey.com", subject="Order 2")
        self.c = self.mail(ME_SENT, subject="Re: Order 2", to=["orders@digikey.com"], folder="Sent")
        EmailMessage.objects.filter(pk__in=[self.a.pk, self.b.pk]).update(is_read=False)
        EmailMessage.objects.filter(pk=self.c.pk).update(folder=EmailMessage.FOLDER_SENT)
        # чужа скринька з тією самою категорією — дії її не чіпають
        other = get_user_model().objects.create_user("bob", "bob@x.example", "x", is_staff=True)
        self.other_box = EmailAccount.objects.create(
            user=other, email_address="bob@x.example", imap_username="bob", imap_password="x",
            smtp_username="bob", smtp_password="x")
        self.foreign = self.mail("orders@digikey.com", subject="Bob order", account=self.other_box)
        EmailMessage.objects.filter(pk=self.foreign.pk).update(is_read=False)

    def post(self, action, **data):
        return self.client.post(f"/email/category/{self.dk.pk}/action/", json.dumps({"action": action, **data}),
                                content_type="application/json")


class CategoryMenuTests(MenuBase):
    def test_info_lists_rules_counts_and_links(self):
        d = self.client.get(f"/email/category/{self.dk.pk}/").json()
        self.assertEqual((d["mails"], d["unread"], d["sent"]), (3, 2, 1))
        self.assertTrue(any("digikey" in r for r in d["rules"]))
        self.assertIn(f"/admin/email_assistant/mailcategory/{self.dk.pk}/change/", d["card_url"])
        self.assertIn(f"?cat={self.dk.pk}", d["dante"]["url"])
        self.assertTrue(d["can_change"] and d["can_delete"])

    def test_inbox_has_menu_and_hide_unhide(self):
        page = self.client.get("/email/")
        self.assertContains(page, 'oncontextmenu="return showWcCtx(event, this)"')
        self.assertContains(page, 'id="em-wc-ctx"')
        self.assertTrue(self.post("hide").json()["ok"])
        self.assertEqual(EmailSettings.get_for_user(self.user).hidden_work_categories, [self.dk.pk])
        page = self.client.get("/email/")
        self.assertContains(page, "em-nav-hid")
        self.assertContains(page, "Сховані")
        self.post("unhide")
        self.assertEqual(EmailSettings.get_for_user(self.user).hidden_work_categories, [])

    def test_mark_all_read_only_own_mailbox(self):
        self.assertEqual(self.post("read").json()["changed"], 2)
        self.assertFalse(EmailMessage.objects.filter(pk__in=[self.a.pk, self.b.pk], is_read=False).exists())
        self.foreign.refresh_from_db()
        self.assertFalse(self.foreign.is_read)

    def test_trash_moves_category_mails_of_this_mailbox(self):
        with mock.patch("email_assistant.views._imap_bg_many") as imap:
            self.assertEqual(self.post("trash").json()["moved"], 3)
        self.assertEqual(len(imap.call_args[0][1]), 3)
        self.assertEqual(EmailMessage.objects.filter(folder=EmailMessage.FOLDER_TRASH).count(), 3)
        self.foreign.refresh_from_db()
        self.assertNotEqual(self.foreign.folder, EmailMessage.FOLDER_TRASH)

    def test_save_rules_and_recount(self):
        with mock.patch("email_assistant.work_categories.start_recompute", return_value=True) as rc:
            d = self.post("save", name="DigiKey Marketplace", icon="🟥", color="#ff0000",
                          senders="digikey\n@digikey.de\nDIGIKEY\n", subject_words="Marketplace").json()
        self.assertTrue(d["recompute"])
        rc.assert_called_once_with(365)
        self.dk.refresh_from_db()
        self.assertEqual((self.dk.name, self.dk.color), ("DigiKey Marketplace", "#ff0000"))
        self.assertEqual(self.dk.senders, "digikey\n@digikey.de")   # без повторів
        with mock.patch("email_assistant.work_categories.start_recompute") as rc:
            d = self.post("save", name="DK", color="red", senders=self.dk.senders,
                          subject_words="Marketplace").json()
        rc.assert_not_called()   # правила ті самі — без перерахунку
        self.dk.refresh_from_db()
        self.assertEqual((self.dk.name, self.dk.color), ("DK", "#ff0000"))   # «red» — не HEX, лишається старий

    def test_delete_keeps_mails(self):
        with mock.patch("email_assistant.work_categories.start_recompute"):
            self.assertTrue(self.post("delete").json()["ok"])
        self.assertFalse(MailCategory.objects.filter(pk=self.dk.pk).exists())
        self.assertEqual(EmailMessage.objects.filter(pk__in=[self.a.pk, self.b.pk], work_category=None).count(), 2)

    def test_without_rights_only_own_view_actions(self):
        clerk = get_user_model().objects.create_user("clerk", "clerk@x.example", "x", is_staff=True)
        self.client.force_login(clerk)
        self.assertEqual(self.post("save", name="X").status_code, 403)
        self.assertEqual(self.post("delete").status_code, 403)
        self.assertTrue(self.post("hide").json()["ok"])
        self.assertTrue(MailCategory.objects.filter(pk=self.dk.pk, name="DigiKey").exists())



class WhyTests(MenuBase):
    """«🔍 Чому лист тут»: правило, що спрацювало, інші категорії, швидка зміна."""

    def why(self, msg):
        return self.client.get(f"/email/message/{msg.pk}/why/").json()

    def test_sender_rule_and_other_matches(self):
        self.shop.senders = "@digikey.com"
        self.shop.save()
        d = self.why(self.a)
        self.assertEqual((d["category"]["pk"], d["how"], d["rule"], d["field"]), (self.dk.pk, "sender", "digikey", "senders"))
        self.assertIn("orders@digikey.com", d["text"])
        self.assertEqual([c["pk"] for c in d["also"]], [self.shop.pk])   # теж підходить, але нижче за порядком
        self.assertFalse(d["stale"])
        self.assertEqual(d["contact"], "orders@digikey.com")

    def test_subject_word_manual_thread_and_none(self):
        m = self.mail("kunde@gmail.com", subject="Ihre Bestellung 7")
        d = self.why(m)
        self.assertEqual((d["how"], d["rule"], d["field"]), ("word", "bestellung", "subject_words"))
        from email_assistant.models import EmailThread
        EmailThread.objects.filter(pk=m.thread_id).update(work_category=self.dk)
        d = self.why(m)
        self.assertEqual(d["how"], "thread")
        self.assertTrue(d["stale"])   # мітка ще «Магазин», після перерахунку — DigiKey
        self.assertEqual(d["expected"]["pk"], self.dk.pk)
        none = self.mail("x@nowhere.example", subject="Hallo")
        self.assertIsNone(self.why(none)["category"])

    def test_remove_matched_rule(self):
        with mock.patch("email_assistant.work_categories.start_recompute", return_value=True):
            d = self.post("remove_line", field="senders", line="DigiKey").json()
        self.assertTrue(d["ok"])
        self.dk.refresh_from_db()
        self.assertEqual(self.dk.senders, "")
        self.assertFalse(self.post("remove_line", field="senders", line="digikey").json()["ok"])

    def test_foreign_mail_is_404(self):
        self.assertEqual(self.client.get(f"/email/message/{self.foreign.pk}/why/").status_code, 404)


class WhyCustomerProjectTests(MenuBase):
    """«🔍 Чому лист тут»: клієнт CRM і проєкти гілки."""

    def setUp(self):
        super().setUp()
        from crm.models import Customer
        self.acme = Customer.objects.create(name="Max Muster", company="ACME GmbH", email="max@acme.example")
        self.raynool = Customer.objects.create(name="Oscar Yang", company="Raynool", email="oscar@raynool.example")

    def set_customer(self, msg, customer):
        return self.client.post(f"/email/message/{msg.pk}/customer/", json.dumps({"customer": customer}),
                                content_type="application/json").json()

    def test_search_and_suggestion_by_address(self):
        self.assertEqual(self.client.get("/email/customers/?q=a").json()["results"], [])
        found = self.client.get("/email/customers/?q=acme").json()["results"]
        self.assertEqual([c["id"] for c in found], [self.acme.pk])
        self.assertIn("ACME GmbH", found[0]["label"])
        m = self.mail("oscar@raynool.example", subject="Cable assemblies")
        d = self.client.get(f"/email/message/{m.pk}/why/").json()
        self.assertEqual(d["thread"], m.thread_id)
        self.assertEqual((d["customer"] or {}).get("id"), self.raynool.pk)   # знайдено за адресою автоматично

    def test_set_customer_moves_thread_mails_into_timeline(self):
        from crm.models import CustomerTimeline
        m1 = self.mail("oscar@raynool.example", subject="Cable assemblies", msg_id="<c1@x>")
        m2 = self.mail("oscar@raynool.example", subject="Re: Cable assemblies", msg_id="<c2@x>", reply_to="<c1@x>")
        self.assertEqual(m1.thread_id, m2.thread_id)
        self.assertEqual(CustomerTimeline.objects.filter(customer=self.raynool).count(), 2)
        d = self.set_customer(m1, self.acme.pk)
        self.assertEqual((d["ok"], d["mails"], d["customer"]["id"]), (True, 2, self.acme.pk))
        m1.thread.refresh_from_db()
        self.assertEqual(m1.thread.crm_customer, self.acme)
        self.assertEqual(CustomerTimeline.objects.filter(customer=self.acme, email_message__in=[m1, m2]).count(), 2)
        self.assertFalse(CustomerTimeline.objects.filter(customer=self.raynool).exists())
        self.assertEqual(self.set_customer(m1, self.acme.pk)["mails"], 2)   # повтор — без дублів
        self.assertEqual(CustomerTimeline.objects.filter(customer=self.acme).count(), 2)
        self.assertIsNone(self.set_customer(m1, "")["customer"])
        m1.thread.refresh_from_db()
        self.assertIsNone(m1.thread.crm_customer)

    def test_no_customer_yet_creates_events(self):
        from crm.models import CustomerTimeline
        m = self.mail("someone@nowhere.example", subject="Anfrage")
        self.assertFalse(CustomerTimeline.objects.filter(email_message=m).exists())
        self.set_customer(m, self.acme.pk)
        ev = CustomerTimeline.objects.get(email_message=m)
        self.assertEqual((ev.customer, ev.event_type, ev.title), (self.acme, "email_in", "Anfrage"))

    def test_foreign_mail_refused_and_projects_listed(self):
        r = self.client.post(f"/email/message/{self.foreign.pk}/customer/", json.dumps({"customer": self.acme.pk}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 404)
        from projects.models import Project
        p = Project.objects.create(name="LTE Board", status=Project.ACTIVE)
        d = self.client.get(f"/email/message/{self.a.pk}/why/").json()
        self.assertIn(p.pk, [x["id"] for x in d["open_projects"]])
        page = self.client.get("/email/")
        self.assertContains(page, "/projects/api/thread/")


class ProjectMenuAndSourcesTests(MenuBase):
    """Меню проєкту в лівій колонці, перейменування, перемикачі джерел над «👤 Клієнти»."""

    def test_project_menu_and_rename(self):
        from projects.models import Project, ProjectLink
        p = Project.objects.create(name="Transponder", status=Project.ACTIVE)
        ProjectLink.objects.create(project=p, thread=self.a.thread, status=ProjectLink.CONFIRMED)
        page = self.client.get("/email/")
        self.assertContains(page, 'oncontextmenu="return showPrCtx(event, this)"')
        self.assertContains(page, "/admin/worktime/workday/mail-time/")
        self.assertContains(page, "wcAction('rename')")
        r = self.client.post(f"/projects/api/project/{p.pk}/rename/", json.dumps({"name": "Transponder 2 LTE"}),
                             content_type="application/json").json()
        self.assertTrue(r["ok"])
        p.refresh_from_db()
        self.assertEqual(p.name, "Transponder 2 LTE")
        self.assertEqual(self.client.post(f"/projects/api/project/{p.pk}/rename/", json.dumps({"name": " "}),
                                          content_type="application/json").status_code, 400)

    def test_customer_sources_chips(self):
        import datetime
        from crm.models import Customer
        from sales.models import SalesOrder, SalesSource
        from email_assistant.views import _customer_sources
        SalesSource.objects.create(slug="digikey", name="DigiKey")
        SalesSource.objects.create(slug="sevskiy-shop", name="Webshop")
        dk = Customer.objects.create(name="DK buyer", email="dk@buyer.example", external_key="k-dk")
        both = Customer.objects.create(name="Both", email="both@buyer.example", external_key="k-both")
        other = Customer.objects.create(name="Other", email="other@buyer.example", external_key="k-other")
        day = datetime.date(2026, 10, 1)
        SalesOrder.objects.create(source="digikey", order_number="D1", order_date=day, customer_key="k-dk")
        SalesOrder.objects.create(source="digikey", order_number="D2", order_date=day, customer_key="k-both")
        SalesOrder.objects.create(source="sevskiy-shop", order_number="S1", order_date=day, email="both@buyer.example")
        customers = [{"id": dk.pk}, {"id": both.pk}, {"id": other.pk}]
        chips = _customer_sources(customers)
        self.assertEqual([(c["key"], c["label"], c["n"]) for c in chips],
                         [("digikey", "DigiKey", 2), ("sevskiy-shop", "Webshop", 1), ("other", "інші", 1)])
        self.assertEqual([c["src"] for c in customers], [["digikey"], ["digikey", "sevskiy-shop"], []])
        self.assertEqual(_customer_sources([]), [])
