"""➕ Додати отримувачів до вже надісланого запиту: видно, кому піде лист (контакт, email, країна), без email — не
вибрати, вибраний контакт — у запит, «📤 Надіслати» — лише доданим (хто вже отримав — ні).

python manage.py test suppliers.tests_rfq_more --settings=tabele.settings_test
"""
import datetime
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from inventory.models import Supplier
from suppliers.models import SupplierContact, SupplierRequest, SupplierRequestRecipient as R
from suppliers.tests import Base


class AddRecipientsTests(Base):
    def setUp(self):
        super().setUp()
        self.req = SupplierRequest.objects.create(
            title="Samples TM270402", template="pcb", category=self.pcb, quantities="5; 2500", language="en",
            owner=self.user, account=self.account, reply_by=timezone.localdate() + datetime.timedelta(days=5),
            status=SupplierRequest.ANSWERED, body="{greeting}\n\nPlease quote.", subject="RFQ")
        self.supplier.categories.add(self.pcb)
        R.objects.create(request=self.req, supplier=self.supplier, email="sales@pcbway.com", state=R.SENT,
                         sent_at=timezone.now())
        self.hong = Supplier.objects.create(name="HongRong (shenzhen) Electronics Co.,Ltd", addr_country="CN")
        self.hong.categories.add(self.pcb)
        self.anna = SupplierContact.objects.create(supplier=self.hong, name="Anna Wu", position="Sales",
                                                   email="anna@hongrong.cn", is_primary=True)
        self.tom = SupplierContact.objects.create(supplier=self.hong, name="Tom Li", email="tom@hongrong.cn")
        self.ecs = Supplier.objects.create(name="ECS Circuits LTD", email="info@ecs.co.uk", addr_country="GB")
        self.ecs.categories.add(self.pcb)
        self.bare = Supplier.objects.create(name="126")                  # ні email, ні контактів
        self.bare.categories.add(self.pcb)
        self.url = reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])
        self.act = reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk])

    def test_list_shows_who_gets_the_letter(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn("Кому піде лист", page)
        self.assertIn("anna@hongrong.cn", page)
        self.assertIn('name="contact_%d"' % self.hong.pk, page)               # кілька контактів — вибір
        self.assertIn("✉️ info@ecs.co.uk", page)
        self.assertIn("(email фірми)", page)
        self.assertIn("немає email — допишіть", page)
        self.assertIn('value="%d" id="rw-more-%d" disabled' % (self.bare.pk, self.bare.pk), page)
        self.assertIn("Китай", page)
        self.assertNotIn(f'id="rw-more-{self.supplier.pk}"', page)              # уже в запиті

    def test_add_with_chosen_contact_and_send_only_to_new(self):
        self.client.post(self.act, {"do": "add", "rcp": [self.hong.pk, self.bare.pk],
                                    f"contact_{self.hong.pk}": self.tom.pk})
        r = self.req.recipients.get(supplier=self.hong)
        self.assertEqual((r.email, r.contact_id, r.state), ("tom@hongrong.cn", self.tom.pk, R.PLANNED))
        self.assertFalse(self.req.recipients.filter(supplier=self.bare).exists())
        sent = []

        def fake_send(client, to_emails, subject, body_text, **kw):
            sent.append(to_emails)
            return {"ok": True, "message_id": f"<more{len(sent)}@sevskiy.de>"}
        with mock.patch("email_assistant.smtp_client.SMTPClient.send", fake_send):
            r = self.client.post(self.act, {"do": "send"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(sent, [["tom@hongrong.cn"]])                       # лише доданому; PCBWay — ні
