"""CRM: фільтр «Тип клієнта» за замовленнями і злиття дублікатів (crm/utils.py, crm/merge.py).

    python manage.py test crm --settings=tabele.settings_test
"""
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from crm import merge, utils
from crm.models import Customer, CustomerNote, CustomerTimeline
from sales.models import SalesOrder


def _ready_admin(test):
    from config.models import SystemSettings
    ss = SystemSettings.objects.get_or_create(pk=1)[0]
    ss.is_onboarding_complete = True
    ss.save()
    test.user = User.objects.create_user("boss", password="x", is_staff=True, is_superuser=True)
    test.client.force_login(test.user)


class CustomerTypeFilterTests(TestCase):
    """«Тип клієнта»: постійний — від 2 замовлень клієнта, новий — 0–1 (а не кількість самих клієнтів)."""

    def setUp(self):
        _ready_admin(self)
        self.regular = Customer.objects.create(name="Regular", email="reg@acme.de")
        self.single = Customer.objects.create(name="Single", email="one@acme.de")
        self.empty = Customer.objects.create(name="Empty", email="none@acme.de")
        # одне замовлення під ключем + одне зі старим ключем (клієнта з ним уже немає) — веде сюди за email
        self.mixed = Customer.objects.create(name="Mixed", email="mix@acme.de")
        self.n = 0
        self.order(self.regular)
        self.order(self.regular)
        self.order(self.single)
        self.order(self.mixed)
        self.order(None, customer_key="stale-key-of-deleted", email="MIX@acme.de ")

    def order(self, customer, **kw):
        self.n += 1
        values = {"source": "webshop", "order_number": f"10{self.n:04d}"}
        if customer is not None:
            values.update(customer_key=customer.external_key, email=customer.email)
        values.update(kw)
        return SalesOrder.objects.create(**values)

    def test_counts_orders_by_key_then_email(self):
        counts = utils.order_counts()
        self.assertEqual(counts[self.regular.pk], 2)
        self.assertEqual(counts[self.single.pk], 1)
        self.assertEqual(counts[self.empty.pk], 0)
        self.assertEqual(counts[self.mixed.pk], 2)
        # те саме для окремих клієнтів (сторінка списку)
        self.assertEqual(utils.order_counts([self.mixed, self.single]), {self.mixed.pk: 2, self.single.pk: 1})
        self.assertEqual(utils.repeat_customer_ids(), {self.regular.pk, self.mixed.pk})

    def test_order_with_own_key_is_not_counted_twice_by_email(self):
        other = Customer.objects.create(name="Other", email="reg@acme.de")   # та сама адреса, свій ключ
        self.assertEqual(utils.order_counts()[other.pk], 0)
        self.assertEqual(utils.order_counts()[self.regular.pk], 2)

    def _names(self, value):
        r = self.client.get("/admin/crm/customer/", {"repeat": value})
        self.assertEqual(r.status_code, 200)
        return {c.name for c in r.context["cl"].result_list}

    def test_filter_in_list(self):
        self.assertEqual(self._names("yes"), {"Regular", "Mixed"})
        self.assertEqual(self._names("no"), {"Single", "Empty"})

    def test_type_column_matches_filter(self):
        r = self.client.get("/admin/crm/customer/", {"repeat": "yes"})
        rows = {c.name: c._type_orders for c in r.context["cl"].result_list}
        self.assertEqual(rows, {"Regular": 2, "Mixed": 2})
        self.assertContains(r, "✅ Постійний", count=2)
        r = self.client.get("/admin/crm/customer/", {"repeat": "no"})
        self.assertContains(r, "🔸 Новий", count=2)


class CustomerMergeTests(TestCase):
    """Злиття: усе, що вказує на дубліката, переходить до основного клієнта; нічого не губиться й не блокує."""

    def setUp(self):
        _ready_admin(self)
        self.media = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=str(self.media))
        media.enable()
        self.addCleanup(media.disable)

        self.winner = Customer.objects.create(name="Muster GmbH", company="Muster GmbH", email="info@muster.de")
        self.loser = Customer.objects.create(name="Muster", company="MUSTER GmbH", email="sales@muster.de",
                                             phone="+49 1", addr_city="Berlin", notes="Старий запис")
        self.loser_pk = self.loser.pk
        self.order = SalesOrder.objects.create(source="webshop", order_number="200001",
                                               customer_key=self.loser.external_key, email=self.loser.email)
        # замовлення без ключа CRM, яке веде на дубліката лише за email (сигнал CRM без email його не чіпає)
        self.loose_order = SalesOrder.objects.create(source="webshop", order_number="200002")
        SalesOrder.objects.filter(pk=self.loose_order.pk).update(email="Sales@Muster.de")

    def _fill_loser(self):
        """Дубліката посилаються записи з різних застосунків (FK, приховані related_name="+", PROTECT)."""
        from accounting.models import Invoice
        from calendar_app.models import CalendarEvent, MeetingInvite
        from email_assistant.models import EmailAccount, EmailThread
        from offers.models import Offer
        from projects.models import Project, ProjectParticipant
        from rag_assistant.models import EntityHelp, KnowledgeItem, KnowledgeSource
        from shop.models import CustomerNumber, Shop, ShopCustomer
        from strategy.models import CustomerStrategy, StrategyTemplate
        from tasks.models import Task
        from worktime.models import DanteProject, MatchRule

        c, now = self.loser, timezone.now()
        CustomerNote.objects.create(customer=c, subject="Дзвінок")
        CustomerTimeline.objects.create(customer=c, title="Лист")
        CustomerStrategy.objects.create(customer=c, template=StrategyTemplate.objects.create(name="Reactivation"))
        Invoice.objects.create(customer=c)                          # PROTECT — раніше блокував видалення
        Task.objects.create(customer=c, title="Передзвонити")       # CASCADE — раніше видалявся
        account = EmailAccount.objects.create(user=self.user, email_address="me@sevskiy.de", imap_username="me",
                                              imap_password="x", smtp_username="me", smtp_password="x")
        self.thread = EmailThread.objects.create(account=account, subject="Anfrage", crm_customer=c)
        project = Project.objects.create(name="Antenna", customer=c)
        ProjectParticipant.objects.create(project=project, email="sales@muster.de", customer=c)
        Offer.objects.create(customer=c)
        self.item = KnowledgeItem.objects.create(title="Datenblatt", customer=c)
        KnowledgeSource.objects.create(name="NAS", customer=c)
        CalendarEvent.objects.create(user=self.user, title="Meeting", start_at=now, crm_customer=c)
        MeetingInvite.objects.create(user=self.user, uid="uid-1", title="Teams", start_at=now, crm_customer=c)
        dante = DanteProject.objects.create(dante_id="d1", name="Muster", crm_customer=c)
        MatchRule.objects.create(kind=MatchRule.CUSTOMER, customer=c, dante_project=dante)
        shop = Shop.objects.create(name="Web", slug="webshop", is_default=True)
        ShopCustomer.objects.create(shop=shop, email="sales@muster.de", password="x", crm_customer=c)
        CustomerNumber.objects.create(number=31728, crm_customer=c)
        EntityHelp.objects.create(user=self.user, kind="customer", obj_id=c.pk, status="done")   # не FK

    def _pointing_at(self, pk):
        """Скільки записів кожної моделі вказує на клієнта pk (усі зв'язки з crm/merge.relations)."""
        return {model._meta.label: model._base_manager.filter(**{field.name: pk}).count()
                for model, field in merge.relations()
                if model._base_manager.filter(**{field.name: pk}).exists()}

    def test_every_relation_moves_to_winner(self):
        self._fill_loser()
        before = self._pointing_at(self.loser_pk)
        self.assertGreaterEqual(len(before), 17)          # усі зв'язки з різних застосунків заповнено
        with self.captureOnCommitCallbacks(execute=True):
            stats = merge.merge_customers(self.winner, [self.loser])

        self.assertFalse(Customer.objects.filter(pk=self.loser_pk).exists())
        self.assertEqual(stats["deleted"], 1)
        after = self._pointing_at(self.winner.pk)
        self.assertEqual(after, before)                    # кожен запис тепер у основного, нічого не видалено
        self.assertEqual(stats["kept"], [])

        from rag_assistant.models import EntityHelp
        self.assertEqual(EntityHelp.objects.filter(kind="customer", obj_id=self.winner.pk).count(), 1)
        self.assertEqual(stats["orders"], 2)
        self.assertEqual(set(SalesOrder.objects.values_list("customer_key", flat=True)), {self.winner.external_key})

        self.winner.refresh_from_db()
        self.assertEqual(self.winner.phone, "+49 1")       # порожні поля — з дубліката
        self.assertEqual(self.winner.addr_city, "Berlin")
        self.assertIn("Старий запис", self.winner.notes)
        self.assertEqual(utils.order_counts([self.winner])[self.winner.pk], 2)

        text = merge.summary(stats)
        self.assertIn("Замовлень перенесено: 2", text)
        self.assertIn("Задачі: 1", text)

    def test_tasks_and_invoices_survive_and_do_not_block(self):
        from accounting.models import Invoice
        from tasks.models import Task
        Invoice.objects.create(customer=self.loser)
        Task.objects.create(customer=self.loser, title="Angebot nachfassen")
        merge.merge_customers(self.winner, [self.loser])
        self.assertEqual(Task.objects.get().customer, self.winner)
        self.assertEqual(Invoice.objects.get().customer, self.winner)

    def test_unique_link_winner_already_has_is_kept_without_customer(self):
        from shop.models import CustomerNumber
        CustomerNumber.objects.create(number=31728, crm_customer=self.winner)
        CustomerNumber.objects.create(number=31729, crm_customer=self.loser)
        stats = merge.merge_customers(self.winner, [self.loser])
        self.assertEqual(CustomerNumber.objects.get(number=31728).crm_customer, self.winner)
        self.assertIsNone(CustomerNumber.objects.get(number=31729).crm_customer)     # запис лишився
        self.assertEqual(len(stats["kept"]), 1)
        self.assertIn("Лишилось без клієнта", merge.summary(stats))

    def test_unique_link_moves_when_winner_has_none(self):
        from shop.models import CustomerNumber
        CustomerNumber.objects.create(number=31729, crm_customer=self.loser)
        merge.merge_customers(self.winner, [self.loser])
        self.assertEqual(CustomerNumber.objects.get().crm_customer, self.winner)

    def test_failure_changes_nothing(self):
        from tasks.models import Task
        Task.objects.create(customer=self.loser, title="X")
        with mock.patch.object(merge, "_fill_winner", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                merge.merge_customers(self.winner, [self.loser])
        self.assertTrue(Customer.objects.filter(pk=self.loser_pk).exists())
        self.assertEqual(Task.objects.get().customer_id, self.loser_pk)
        self.assertEqual(SalesOrder.objects.get(pk=self.order.pk).customer_key, self.loser.external_key)

    def test_documents_folder_and_knowledge_base_follow(self):
        self._fill_loser()
        src = self.media / "customers" / str(self.loser_pk)
        dest = self.media / "customers" / str(self.winner.pk)
        src.mkdir(parents=True)
        dest.mkdir(parents=True)
        (src / "Vertrag.pdf").write_bytes(b"%PDF-loser")
        (dest / "Vertrag.pdf").write_bytes(b"%PDF-winner")
        (src / "AGB.pdf").write_bytes(b"%PDF-agb")
        from email_assistant.models import EmailMessage
        msg = EmailMessage.objects.create(account=self.thread.account, thread=self.thread, subject="Anfrage",
                                          message_id="<m1@muster.de>")
        with mock.patch("email_assistant.knowledge_hook.touch") as mail_touch, \
                mock.patch("rag_assistant.knowledge_items.touch") as doc_touch, \
                self.captureOnCommitCallbacks(execute=True):
            merge.merge_customers(self.winner, [self.loser])
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ["AGB.pdf", "Vertrag (2).pdf", "Vertrag.pdf"])
        self.assertEqual((dest / "Vertrag.pdf").read_bytes(), b"%PDF-winner")      # не перезаписано
        self.assertEqual((dest / "Vertrag (2).pdf").read_bytes(), b"%PDF-loser")
        self.assertFalse(src.exists())
        mail_touch.assert_called_once_with(pks=[msg.pk])
        doc_touch.assert_called_once_with([self.item.pk])

    def test_admin_merge_view(self):
        from tasks.models import Task
        Task.objects.create(customer=self.loser, title="Передзвонити")
        ids = f"{self.winner.pk},{self.loser.pk}"
        r = self.client.get("/admin/crm/customer/merge/", {"ids": ids})
        self.assertContains(r, "Проєкти, гілки пошти, Angebote")
        r = self.client.post("/admin/crm/customer/merge/", {"ids": ids, "winner": self.winner.pk}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Customer.objects.filter(pk=self.loser_pk).exists())
        self.assertEqual(Task.objects.get().customer, self.winner)
        self.assertContains(r, "Замовлень перенесено: 2")

    def test_management_command_moves_everything(self):
        """manage.py merge_duplicate_customers — той самий crm/merge.py + B2B-ключ для primary і його замовлень."""
        from io import StringIO
        from django.core.management import call_command
        from projects.models import Project
        from tasks.models import Task
        Customer.objects.filter(pk=self.loser.pk).update(company="Muster GmbH")
        Task.objects.create(customer=self.loser, title="Передзвонити")
        Project.objects.create(name="Antenna", customer=self.loser)
        own = SalesOrder.objects.create(source="webshop", order_number="200003", customer_key=self.winner.external_key)
        call_command("merge_duplicate_customers", stdout=StringIO())
        survivor = Customer.objects.get()
        self.assertEqual(survivor.external_key, Customer.generate_key("b2b", "Muster GmbH"))
        self.assertEqual(Task.objects.get().customer, survivor)
        self.assertEqual(Project.objects.get().customer, survivor)
        self.assertEqual(SalesOrder.objects.get(pk=own.pk).customer_key, survivor.external_key)
        self.assertEqual(SalesOrder.objects.get(pk=self.order.pk).customer_key, survivor.external_key)

    def test_merge_all_duplicates(self):
        from tasks.models import Task
        Task.objects.create(customer=self.loser, title="Передзвонити")
        r = self.client.post("/admin/crm/customer/find-duplicates/merge-all/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Customer.objects.count(), 1)
        survivor = Customer.objects.get()
        self.assertEqual(Task.objects.get().customer, survivor)
        self.assertEqual(utils.order_counts([survivor])[survivor.pk], 2)
