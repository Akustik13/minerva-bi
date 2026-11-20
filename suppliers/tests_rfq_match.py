"""🔎 Відповідь на запит окремим листом / з іншої адреси / без номера запиту: впізнати за номером деталі з запиту й
назвою (адресою) фірми, «🔎 Знайти пропущені відповіді», AI читає все листування (і наші листи, і прив'язані гілки).

python manage.py test suppliers.tests_rfq_match --settings=tabele.settings_test
"""
import datetime
import json
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from email_assistant.models import EmailThread
from inventory.models import Supplier
from suppliers.models import SupplierContact, SupplierEvent, SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import ai, rfq, rfq_match
from suppliers.tests import Base


class MatchTests(Base):
    def setUp(self):
        super().setUp()
        t0 = timezone.now() - datetime.timedelta(days=7)
        self.req = SupplierRequest.objects.create(
            title="Sampels 5pcs. TM270402-04H", template="pcba", quantities="5; 2500", language="en", owner=self.user,
            account=self.account, status=SupplierRequest.ANSWERED, sent_at=t0, body="{greeting}\n\nPlease quote.")
        rfq.add_file(self.req, SimpleUploadedFile("TM270402-04H _4L_7628_1x34x85.zip", b"PK zip"), self.user)
        self.jy = Supplier.objects.create(name="JY Electronics", email="")
        SupplierContact.objects.create(supplier=self.jy, name="Liu Wenlong", email="charles@jypcba.com")
        self.hi = Supplier.objects.create(name="HITECH CIRCUITS CO.,LTD", email="sales8@hitechpcb.com")
        th = EmailThread.objects.create(account=self.account, subject="RFQ")
        self.r_jy = R.objects.create(request=self.req, supplier=self.jy, email="charles@jypcba.com", state=R.REPLIED,
                                     thread=th, sent_at=t0)
        self.first = self.mail(frm="charles@jypcba.com", subject=f"Re: [{self.req.code}] RFQ", thread=th,
                               body="We will quote soon.", when=t0 + datetime.timedelta(hours=3))
        self.r_hi = R.objects.create(request=self.req, supplier=self.hi, email="sales8@hitechpcb.com", state=R.SENT,
                                     thread=EmailThread.objects.create(account=self.account, subject="RFQ hi"),
                                     sent_at=t0)

    def frank(self, **kw):
        th = EmailThread.objects.create(account=self.account, subject="TM270402-04H Quotation")
        args = dict(frm="frank@bld-pcb.com", name="frank", subject="TM270402-04H _4L_7628 Quotation --JY Electronics",
                    body="Dear Dr. Viacheslav Pryimak,\nPlease find our quotations for TM270402-04H _4L_7628 attached.",
                    thread=th)
        args.update(kw)
        return self.mail(**args)

    def test_keys_and_names(self):
        self.assertEqual(rfq_match.keys(self.req), {"tm27040204h"})                  # без «1x34x85», «4L_7628»
        self.assertIn("jyelectronics", rfq_match.name_variants("JY Electronics"))
        self.assertIn("hkvictory", rfq_match.name_variants("HK Victory Electronic Technology Co., Ltd"))
        self.assertEqual(rfq_match.name_variants("SVI"), set())                       # закоротко — ні

    def test_separate_letter_from_other_address_is_linked(self):
        m = self.frank()
        self.assertEqual(rfq.on_incoming(m), 1)
        self.assertEqual(list(self.r_jy.extra_threads.values_list("pk", flat=True)), [m.thread_id])
        self.assertTrue(SupplierEvent.objects.filter(request=self.req, message=m).exists())
        self.assertFalse(self.r_hi.extra_threads.exists())
        from suppliers.services import rfq_replies
        self.assertIn(m.pk, [x.pk for x in rfq_replies.mail(self.r_jy)])
        # наступний лист у тій самій новій гілці — за гілкою, без дубля
        again = self.mail(frm="frank@bld-pcb.com", subject="Re: TM270402-04H Quotation", thread=m.thread,
                          body="Updated price attached.")
        rfq.on_incoming(again)
        self.assertEqual(self.r_jy.extra_threads.count(), 1)
        # новий лист після розбору AI — розібрати знову
        R.objects.filter(pk=self.r_jy.pk).update(ai={"v": 2, "summary": "x",
                                                     "at": (timezone.now() - datetime.timedelta(hours=1)).isoformat()})
        from suppliers.admin import SupplierRequestAdmin
        self.assertIn(self.r_jy.pk, SupplierRequestAdmin._to_parse(self.req, False))

    def test_unknown_firm_goes_to_missed_then_link_or_skip(self):
        m = self.frank(subject="Quotation TM270402-04H", body="See attached quotation.", name="Frank")
        self.assertEqual(rfq.on_incoming(m), 0)                                       # фірма невідома — не вгадуємо
        found = rfq_match.missed(self.req, self.user)
        self.assertEqual([x["msg"].pk for x in found], [m.pk])
        self.assertIn("номер з запиту «TM27040204H»", found[0]["reasons"])
        self.assertIsNone(found[0]["rid"])
        page = self.client.get(reverse("admin:suppliers_supplierrequest_missed", args=[self.req.pk])).json()
        self.assertEqual(page["items"][0]["msg"], m.pk)
        self.assertEqual({x["name"] for x in page["recipients"]}, {"JY Electronics", "HITECH CIRCUITS CO.,LTD"})
        act = reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk])
        j = self.client.post(act, {"do": "link_msg", "msg": m.pk, "rid": self.r_jy.pk}).json()
        self.assertTrue(j["linked"])
        self.assertEqual(rfq_match.missed(self.req, self.user), [])
        other = self.frank(subject="TM270402-04H — newsletter", body="Our new factory TM270402-04H")
        self.client.post(act, {"do": "skip_msg", "msg": other.pk})
        self.req.refresh_from_db()
        self.assertEqual(rfq_match.missed(self.req, self.user), [])
        # чужий лист (не зі своїх скриньок) — не прив'язати
        foreign = self.mail(frm="x@bld-pcb.com", subject="TM270402-04H", account=self.other_account)
        self.assertEqual(self.client.post(act, {"do": "link_msg", "msg": foreign.pk, "rid": self.r_jy.pk}).status_code, 404)

    def test_letter_from_supplier_domain_in_new_thread_is_missed_candidate(self):
        th = EmailThread.objects.create(account=self.account, subject="Our quotation")
        m = self.mail(frm="susie@hitechpcb.com", subject="Our quotation", body="Price attached.", thread=th)
        found = rfq_match.missed(self.req, self.user)
        hit = next(x for x in found if x["msg"].pk == m.pk)
        self.assertEqual(hit["rid"], self.r_hi.pk)
        self.assertIn("відправник — HITECH CIRCUITS CO.,LTD", hit["reasons"])

    def test_ai_reads_whole_correspondence_with_our_letters(self):
        m = self.frank()
        rfq.on_incoming(m)
        self.mail(frm=self.account.email_address, subject="Re: TM270402-04H Quotation", thread=m.thread,
                  body="Can you do 0.40 USD for 2500 pcs?", folder="sent")
        self.mail(frm="frank@bld-pcb.com", subject="Re: TM270402-04H Quotation", thread=m.thread,
                  body="OK, 0.40 USD for 2500 pcs confirmed.")
        answer = json.dumps({"currency": "USD", "tiers": [{"qty": 2500, "unit_price": 0.40}], "summary": "ok"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.01"))) as call:
            ai.quote(self.r_jy, user=self.user)
        text = call.call_args[0][1]
        self.assertIn("📤 НАШ лист", text)
        self.assertIn("Can you do 0.40 USD", text)
        self.assertIn("0.40 USD for 2500 pcs confirmed", text)
        self.assertIn("We will quote soon.", text)                                     # і перша гілка
        self.assertIn("НАШ лист", call.call_args[0][0])                                # правило в системному
