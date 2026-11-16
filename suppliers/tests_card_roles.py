"""👥 Візитки: хто це — постачальник / клієнт / інше; пресет, AI-підказка, фільтр, «👤 У CRM», API додатка.

python manage.py test suppliers.tests_card_roles --settings=tabele.settings_test
"""
import json
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from crm.models import Customer
from inventory.models import Supplier
from suppliers.models import SupplierCardPreset, SupplierMedia
from suppliers.services import ai, card_app, cards
from suppliers.tests import Base, png


class CardRoleTests(Base):
    def card(self, **kw):
        return cards.create(self.user, SimpleUploadedFile("c.png", png(), "image/png"), **kw)

    def read(self, m, extracted):
        SupplierMedia.objects.filter(pk=m.pk).update(extracted=extracted, state=SupplierMedia.READ)
        m.refresh_from_db()
        return m

    def test_role_from_form_preset_and_supplier(self):
        self.assertEqual(self.card(role="customer").role, "customer")
        self.assertEqual(self.card(role="nonsense").role, "")
        p = SupplierCardPreset.objects.create(owner=self.user, name="наш стенд", role="customer")
        self.assertEqual(self.card(preset_id=p.pk).role, "customer")
        self.assertEqual(self.card(preset_id=p.pk, role="other").role, "other")       # вибране людиною важливіше
        self.assertEqual(self.card(supplier=self.supplier).role, "supplier")

    def test_ai_suggests_role_only_when_not_chosen(self):
        answer = json.dumps({"company": "Antenna Systems GmbH", "contact": {"name": "Max", "email": "max@as.de"},
                             "role": "customer", "role_why": "Інтегрує антени в обладнання"})
        m = self.card()
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.001"))):
            ai.card(m, user=self.user)
        m.refresh_from_db()
        self.assertEqual((m.role, m.role_ai, m.extracted["role_why"]), ("customer", True, "Інтегрує антени в обладнання"))
        self.assertIn("· 🧠", cards.role_label(m))
        chosen = self.card(role="supplier")
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.001"))):
            ai.card(chosen, user=self.user)
        chosen.refresh_from_db()
        self.assertEqual((chosen.role, chosen.role_ai), ("supplier", False))          # людину AI не перебиває
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.001"))) as call:
            ai.card(self.card(), user=self.user)
        self.assertIn("role_why", call.call_args[0][0])                                # просимо в промпті

    def test_bulk_to_crm_and_suppliers_respect_role(self):
        a = self.read(self.card(role="customer"), {"name": "Antenna Systems GmbH", "contact_name": "Max Muster",
                                                   "contact_email": "max@antenna-systems.de"})
        b = self.read(self.card(role="customer"), {"name": "Antenna Systems GmbH", "contact_name": "Eva",
                                                   "contact_email": "max@antenna-systems.de"})
        empty = self.card(role="customer")
        out = cards.to_customers([a, b, empty], self.user)
        self.assertEqual(len(out["created"]), 1)
        self.assertEqual(len(out["attached"]), 1)                                      # той самий email — без дубля
        self.assertEqual(Customer.objects.filter(email__iexact="max@antenna-systems.de").count(), 1)
        self.assertTrue(any("немає даних" in x for x in out["skipped"]))
        a.refresh_from_db()
        self.assertEqual((a.role, a.customer.company), ("customer", "Antenna Systems GmbH"))
        again = cards.to_customers([a], self.user)
        self.assertEqual(again["skipped"], [f"{a.title or 'Antenna Systems GmbH'}: уже в CRM"])
        # клієнта «🏭 У постачальники» не переносить
        c = self.read(self.card(role="customer"), {"name": "Buyer Ltd", "email": "info@buyer.com"})
        out = cards.to_suppliers([c], self.user)
        self.assertFalse(out["created"])
        self.assertIn("👤 Клієнт", out["skipped"][0])
        s = self.read(self.card(), {"name": "Quartz Inc", "email": "sales@quartz.cn"})
        out = cards.to_suppliers([s], self.user)
        s.refresh_from_db()
        self.assertEqual((len(out["created"]), s.role), (1, "supplier"))

    def test_list_filter_badge_and_bulk_action(self):
        self.read(self.card(role="customer", title="Buyer card"), {"name": "Buyer Ltd", "email": "info@buyer.com"})
        self.card(role="supplier", title="Supplier card")
        self.card(title="Nobody card")
        url = reverse("admin:inventory_supplier_cards")
        page = self.client.get(url, {"role": "customer"}).content.decode()
        self.assertIn("Buyer card", page)
        self.assertNotIn("Supplier card", page)
        self.assertIn("👤 Клієнт (1)", page)
        self.assertIn('class="spc-role spc-role-customer"', page)
        everything = self.client.get(url).content.decode()                            # без фільтра — усі
        self.assertTrue(all(t in everything for t in ("Buyer card", "Supplier card", "Nobody card")))
        self.assertNotIn("Buyer card", self.client.get(url, {"role": "-"}).content.decode())
        self.assertIn("Nobody card", self.client.get(url, {"role": "-"}).content.decode())
        m = SupplierMedia.objects.get(title="Buyer card")
        self.client.post(url, {"action": "to_customers", "ids": [m.pk]})
        m.refresh_from_db()
        self.assertIsNotNone(m.customer_id)

    def test_review_page_saves_role_and_shows_crm_first(self):
        m = self.card()
        SupplierMedia.objects.filter(pk=m.pk).update(role="customer", role_ai=True)
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        page = self.client.get(url).content.decode()
        self.assertIn('class="cr-customer"', page)
        self.assertIn("🧠 AI: 👤 Клієнт", page)
        self.client.post(url, {"action": "note", "role": "supplier", "note": "", "event": "", "title": ""})
        m.refresh_from_db()
        self.assertEqual((m.role, m.role_ai), ("supplier", False))

    def test_app_api_role_filter_edit_and_to_customer(self):
        from suppliers.models import CardAppDevice
        token = "mvc_test-token-1"
        import hashlib
        CardAppDevice.objects.create(user=self.user, name="phone", token_hash=hashlib.sha256(token.encode()).hexdigest())
        h = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        meta = self.client.get("/suppliers/app/api/meta/", **h).json()
        self.assertIn(["customer", "👤 Клієнт"], meta["roles"])
        self.assertTrue(meta["perms"]["crm_add"])
        up = self.client.post("/suppliers/app/api/cards/", {"image": SimpleUploadedFile("c.png", png(), "image/png"),
                                                             "uuid": "u-1", "role": "customer"}, **h).json()
        cid = up["card"]["id"]
        self.assertEqual(up["card"]["role"], "customer")
        items = self.client.get("/suppliers/app/api/cards/", {"role": "customer"}, **h).json()["items"]
        self.assertEqual([x["id"] for x in items], [cid])
        cards.create(self.user, SimpleUploadedFile("d.png", png(), "image/png"), role="supplier")
        self.assertEqual(len(self.client.get("/suppliers/app/api/cards/", {"role": ""}, **h).json()["items"]), 2)
        self.client.post(f"/suppliers/app/api/cards/{cid}/", json.dumps({"role": "other"}),
                         content_type="application/json", **h)
        self.assertEqual(SupplierMedia.objects.get(pk=cid).role, "other")
        SupplierMedia.objects.filter(pk=cid).update(extracted={"name": "Buyer Ltd", "email": "info@buyer.com"})
        res = self.client.post(f"/suppliers/app/api/cards/{cid}/to-customer/", **h).json()
        self.assertTrue(res["created"])
        self.assertEqual(res["card"]["role"], "customer")
        self.assertEqual(res["card"]["customer"]["name"], "Buyer Ltd")

    def test_preset_saves_role(self):
        out = card_app.save_preset(self.user, {"action": "save", "name": "наш стенд", "role": "customer"})
        self.assertEqual(next(p for p in out["presets"] if p["id"] == out["id"])["role"], "customer")


class RoleMigrationTests(Base):
    def test_existing_cards_get_role_from_where_they_went(self):
        import importlib
        from django.apps import apps
        mig = importlib.import_module("suppliers.migrations.0011_card_role")
        s = cards.create(self.user, SimpleUploadedFile("a.png", png(), "image/png"), supplier=self.supplier)
        SupplierMedia.objects.filter(pk=s.pk).update(role="")
        cust = Customer.objects.create(name="Buyer", email="b@buyer.com")
        c = cards.create(self.user, SimpleUploadedFile("b.png", png(), "image/png"))
        SupplierMedia.objects.filter(pk=c.pk).update(customer=cust)
        n = cards.create(self.user, SimpleUploadedFile("c.png", png(), "image/png"))
        mig.fill_roles(apps, None)
        self.assertEqual([SupplierMedia.objects.get(pk=x.pk).role for x in (s, c, n)], ["supplier", "customer", ""])
        self.assertTrue(Supplier.objects.exists())
