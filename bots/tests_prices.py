"""💲 Ціни DigiKey ← склад Minerva (bots/services/dk_prices.py). DigiKey імітується — без мережі.
python manage.py test bots --settings=tabele.settings_test"""
import copy
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from bots.models import (DigiKeyConfig, DigiKeyListing, DigiKeyPriceChange, DigiKeyPriceLog, DigiKeyPriceRun,
                         DigiKeyPriceSettings)
from bots.services import dk_prices as svc
from inventory.models import Product

D = Decimal


class FakeDigiKey:
    """Офери DigiKey у пам'яті: GET /offers і PUT /offers/{id}."""

    def __init__(self):
        self.offers = {}
        self.puts = []
        self.apply = True          # False — DigiKey прийняв PUT, але GET ще показує старі ціни
        self.fail = {}
        self.lists = self.singles = 0  # скільки разів читали весь список / один офер

    def add(self, offer_id, sku, prices, **extra):
        self.offers[offer_id] = {
            "id": offer_id, "supplierSku": sku, "title": f"DK {sku}", "description": "desc",
            "prices": [{"quantityBreak": q, "price": p} for q, p in prices], "quantityAvailable": 77,
            "minOrderQuantity": 1, "leadTimeToShip": 3, "isActive": True, "isAvailable": True,
            "maxOrderQuantity": 500, "additionalFields": [{"code": "x", "value": "y"}],
            "lastUpdateDateUtc": "2026-10-01T10:00:00Z", "lastUpdatedSource": "API", **extra}

    def fetch(self):
        self.lists += 1
        return copy.deepcopy(list(self.offers.values()))

    def one(self, sku):
        self.singles += 1
        return next((copy.deepcopy(o) for o in self.offers.values() if o["supplierSku"] == sku), None)

    def put(self, offer_id, payload):
        if offer_id in self.fail:
            raise Exception(self.fail[offer_id])
        self.puts.append((offer_id, copy.deepcopy(payload)))
        if self.apply:
            self.offers[offer_id]["prices"] = payload["prices"]
            self.offers[offer_id]["lastUpdateDateUtc"] = timezone.now().isoformat()
        return {"id": offer_id, "prices": payload["prices"]}


class PriceBase(TestCase):
    def setUp(self):
        svc.BACKGROUND = False
        from shop.models import ShopSettings
        ss = ShopSettings.get()
        ss.fx_rates = {"USD": "0.9"}
        ss.price_breaks = [{"min_qty": 10, "discount": 10}, {"min_qty": 100, "discount": 20}]
        ss.save()
        DigiKeyConfig.objects.update_or_create(pk=1, defaults={"locale_currency": "EUR"})
        self.dk = FakeDigiKey()
        from django.core.cache import cache
        cache.clear()
        patches = [mock.patch.object(svc, "_fetch_offers", self.dk.fetch),
                   mock.patch.object(svc, "_fetch_one", self.dk.one),
                   mock.patch.object(svc, "_put_offer", self.dk.put)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.s = DigiKeyPriceSettings.get()

    def product(self, sku, prices=None, currency="EUR", **kw):
        return Product.objects.create(sku=sku, name=f"Name {sku}", base_prices=[
            {"min_qty": q, "unit_price": str(p)} for q, p in (prices or [])], base_price_currency=currency, **kw)

    def listing(self, product, offer_id, dk_prices, status="published", **kw):
        self.dk.add(offer_id, product.sku, dk_prices)
        return DigiKeyListing.objects.create(
            product=product, dk_offer_id=offer_id, sync_status=status, dk_title=f"DK {product.sku}",
            dk_prices=[{"qty": q, "price": p} for q, p in dk_prices], **kw)

    def row(self, listing, **rules):
        return svc.row_for(listing, svc.Rules.from_dict(rules, svc.Rules.from_settings()), "EUR")


class PlanTests(PriceBase):
    def test_currency_conversion_rules_and_direction(self):
        l = self.listing(self.product("AN-1", [(1, 10), (10, 9)], "USD"), "o1", [(1, 8.5), (10, 8.0)])
        r = self.row(l)
        self.assertEqual(r.new, [(1, D("9.00")), (10, D("8.10"))])         # 10 USD × 0,9
        self.assertTrue(r.differs)
        self.assertEqual(r.direction, "up")
        self.assertEqual(r.change_pct, D("5.88"))
        r = self.row(l, adjust_pct="-10", adjust_fixed="0.5")
        self.assertEqual(r.new, [(1, D("8.60")), (10, D("7.79"))])          # 9 × 0,9 + 0,5
        r = self.row(l, rounding="x.99")
        self.assertEqual(r.new, [(1, D("8.99")), (10, D("7.99"))])
        self.assertEqual(r.direction, "mixed")

    def test_tier_modes_moq_floor_and_own_adjustment(self):
        p = self.product("AN-2", [(1, 10), (50, 8)], purchase_price=D("9"))
        l = self.listing(p, "o2", [(1, 11), (5, 10.5), (25, 10)], dk_min_order_qty=1)
        self.assertEqual(self.row(l, tiers_mode="digikey").new, [(1, D("10.00")), (5, D("10.00")), (25, D("10.00"))])
        self.assertEqual(self.row(l, tiers_mode="template").new, [(1, D("10.00")), (10, D("9.00")), (100, D("8.00"))])
        floor = self.row(l, min_margin_pct="10")
        self.assertEqual(floor.new, [(1, D("10.00")), (50, D("9.90"))])     # 8 < 9 × 1,1
        self.assertTrue(any("закупівельної" in n for n in floor.notes))
        l.dk_min_order_qty = 5
        r = self.row(l)
        self.assertEqual(r.new, [(5, D("10.00")), (50, D("8.00"))])
        self.assertTrue(any("MOQ" in n for n in r.notes))
        l.dk_min_order_qty, l.price_adjust_pct, l.price_adjust_fixed = 1, D("-5"), D("0.25")
        r = self.row(l, adjust_pct="20")
        self.assertEqual(r.new, [(1, D("9.75")), (50, D("7.85"))])          # своя −5 % замість +20 %
        self.assertIn("своя", r.notes[0])

    def test_states(self):
        staged = self.listing(self.product("S-1", [(1, 5)]), "s1", [(1, 5)], status="staged")
        self.assertEqual(self.row(staged).state, "staged")
        self.assertEqual(self.row(self.listing(self.product("N-1"), "n1", [(1, 5)])).state, "no_prices")
        sale = self.listing(self.product("P-1", sale_price=D("7")), "p1", [(1, 6)])
        self.assertEqual(self.row(sale).new, [(1, D("7.00"))])
        self.assertEqual(self.row(sale, source="base").state, "no_prices")
        gbp = self.listing(self.product("G-1", [(1, 5)], "GBP"), "g1", [(1, 6)])
        r = self.row(gbp)
        self.assertEqual(r.state, "no_rate")
        self.assertIn("GBP", r.state_text)
        same = self.listing(self.product("E-1", [(1, 6)]), "e1", [(1, 6)])
        self.assertFalse(self.row(same).differs)
        summary = svc.summary(svc.plan())
        self.assertEqual((summary["differs"], summary["same"]), (1, 1))
        with self.assertRaises(ValueError):
            svc.Rules.from_dict({"adjust_pct": "abc"})


class PushTests(PriceBase):
    def setUp(self):
        super().setUp()
        from config.models import NotificationSettings
        ns = NotificationSettings.get()
        ns.telegram_enabled, ns.telegram_bot_token, ns.telegram_chat_id = True, "t", "c"
        ns.email_enabled, ns.email_to = True, "boss@example.com"
        ns.save()
        self.tg = mock.patch("dashboard.notifications._send_telegram").start()
        self.addCleanup(mock.patch.stopall)
        from django.core.mail import get_connection
        mock.patch("dashboard.notifications._smtp_connection",
                   lambda ns: get_connection("django.core.mail.backends.locmem.EmailBackend")).start()
        self.l = self.listing(self.product("AN-1", [(1, 10), (10, 9)]), "o1", [(1, 8.5), (10, 8.0)])

    def run_now(self, user="anna"):
        run = svc.create_run(svc.plan(), user=user)
        svc.start(run)
        run.refresh_from_db()
        return run

    def test_push_changes_only_prices_then_verify(self):
        run = self.run_now()
        self.assertEqual(run.status, DigiKeyPriceRun.SENT)
        offer_id, payload = self.dk.puts[0]
        self.assertEqual(offer_id, "o1")
        self.assertEqual(payload["prices"], [{"quantityBreak": 1, "price": 10.0}, {"quantityBreak": 10, "price": 9.0}])
        self.assertEqual((payload["quantityAvailable"], payload["title"], payload["maxOrderQuantity"]), (77, "DK AN-1", 500))
        self.assertNotIn("additionalFields", payload)
        line = run.lines.get()
        self.assertEqual((line.status, line.old_prices[0]["price"]), (DigiKeyPriceChange.SENT, "8.5"))
        self.l.refresh_from_db()
        self.assertEqual(self.l.dk_prices[0]["price"], 10.0)
        self.assertEqual(DigiKeyPriceLog.objects.get().user, "💲 anna")
        self.assertIn("ціни оновлено", self.tg.call_args[0][1])
        self.assertIn("AN-1", mail.outbox[0].alternatives[0][0])

        self.assertEqual(svc.verify()["checked"], 0)                        # ще не минула хвилина
        res = svc.verify(force=True)
        self.assertEqual(res["verified"], 1)
        run.refresh_from_db()
        self.assertEqual((run.status, run.verified, run.notified_verified), (DigiKeyPriceRun.VERIFIED, 1, True))
        self.assertIn("нові ціни діють", self.tg.call_args[0][1])
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(svc.verify(force=True)["checked"], 0)

    def test_waits_for_digikey_then_mismatch(self):
        self.dk.apply = False
        run = self.run_now()
        self.assertEqual(svc.verify(force=True)["verified"], 0)
        self.assertEqual(run.lines.get().status, DigiKeyPriceChange.SENT)
        DigiKeyPriceChange.objects.update(sent_at=timezone.now() - timedelta(hours=25))
        self.assertEqual(svc.verify(force=True)["mismatch"], 1)
        run.refresh_from_db()
        self.assertEqual(run.status, DigiKeyPriceRun.FAILED)
        self.assertIn("24 год", run.lines.get().error)

    def test_manual_change_on_digikey_is_mismatch(self):
        self.dk.apply = False
        run = self.run_now()
        self.dk.offers["o1"].update(lastUpdatedSource="Manual",
                                    lastUpdateDateUtc=(timezone.now() + timedelta(minutes=5)).isoformat(),
                                    prices=[{"quantityBreak": 1, "price": 12}])
        svc.verify(run, force=True)
        line = run.lines.get()
        self.assertEqual(line.status, DigiKeyPriceChange.MISMATCH)
        self.assertIn("Manual", line.error)

    def test_already_same_error_and_missing_offer(self):
        other = self.listing(self.product("AN-2", [(1, 5)]), "o2", [(1, 4)])
        gone = self.listing(self.product("AN-3", [(1, 5)]), "o3", [(1, 4)])
        rows = svc.plan()
        self.dk.offers["o1"]["prices"] = [{"quantityBreak": 1, "price": 10}, {"quantityBreak": 10, "price": 9}]
        self.dk.fail["o2"] = "update_offer 400: bad"
        del self.dk.offers["o3"]
        run = svc.create_run(rows, user="anna")
        svc.start(run)
        run.refresh_from_db()
        st = dict(run.lines.values_list("sku", "status"))
        self.assertEqual(st, {"AN-1": "verified", "AN-2": "error", "AN-3": "error"})
        self.assertEqual(run.status, DigiKeyPriceRun.PARTIAL)
        self.assertIn("400", run.lines.get(sku="AN-2").error)
        self.assertFalse(self.dk.puts)
        other.refresh_from_db(), gone.refresh_from_db()
        self.assertEqual(other.dk_prices[0]["price"], 4)                     # невдале — локально без змін

    def test_payload_without_quantity_keeps_stock(self):
        offer = self.dk.fetch()[0]
        del offer["quantityAvailable"]
        self.l.dk_quantity_override = 12
        payload = svc.offer_payload(offer, [(1, D("10"))], self.l)
        self.assertEqual(payload["quantityAvailable"], 12)
        offer["minOrderQuantity"] = 0
        self.assertEqual(svc.offer_payload(offer, [(1, D("10"))], self.l)["minOrderQuantity"], 1)

    def test_create_run_without_differences(self):
        DigiKeyListing.objects.update(dk_prices=[{"qty": 1, "price": 10}, {"qty": 10, "price": 9}])
        with self.assertRaises(svc.PriceError):
            svc.create_run(svc.plan())


class AutoTests(PriceBase):
    def setUp(self):
        super().setUp()
        self.s.auto_mode, self.s.max_change_pct, self.s.notify_telegram, self.s.notify_email = "apply", D("20"), False, False
        self.s.save()
        self.small = self.listing(self.product("A-1", [(1, "10.50")]), "a1", [(1, 10)])   # +5 %
        self.big = self.listing(self.product("B-1", [(1, 15)]), "b1", [(1, 10)])         # +50 %

    def test_apply_guard_suppress_and_new_price(self):
        svc.auto()
        run = DigiKeyPriceRun.objects.get()
        st = dict(run.lines.values_list("sku", "status"))
        self.assertEqual(st, {"A-1": "sent", "B-1": "held"})
        self.assertIn("50", run.lines.get(sku="B-1").notes[-1])
        self.assertEqual([p[0] for p in self.dk.puts], ["a1"])
        self.assertIn("чекає рішення", svc.auto())
        self.assertEqual(DigiKeyPriceRun.objects.count(), 1)                # B-1 уже чекає — без дубля
        svc.reject(run, [run.lines.get(sku="B-1").pk], "anna")
        svc.auto()
        self.assertEqual(DigiKeyPriceRun.objects.count(), 1)                # відхилене не пропонується
        p = self.big.product
        p.base_prices = [{"min_qty": 1, "unit_price": "11"}]
        p.base_prices_updated_at = timezone.now()
        p.save()
        svc.auto()
        self.assertEqual(DigiKeyPriceRun.objects.count(), 1)                # щойно змінено — чекаємо
        Product.objects.filter(pk=p.pk).update(base_prices_updated_at=timezone.now() - timedelta(hours=1))
        svc.auto()
        self.assertEqual(DigiKeyPriceRun.objects.count(), 2)
        self.assertEqual(DigiKeyPriceRun.objects.first().lines.get().status, "sent")

    def test_ask_mode_then_confirm(self):
        self.s.auto_mode = "ask"
        self.s.save()
        svc.auto()
        run = DigiKeyPriceRun.objects.get()
        self.assertEqual(run.status, DigiKeyPriceRun.HELD)
        self.assertFalse(self.dk.puts)
        self.assertEqual(svc.confirm(run, list(run.lines.values_list("pk", flat=True)), "anna"), 2)
        run.refresh_from_db()
        self.assertEqual((run.status, run.sent), (DigiKeyPriceRun.SENT, 2))

    def test_off_and_cron(self):
        self.s.auto_mode = "off"
        self.s.save()
        self.assertEqual(svc.auto(), "автооновлення вимкнено")
        call_command("dk_price_sync", "--if-due", stdout=mock.MagicMock())
        self.s.refresh_from_db()
        self.assertIsNotNone(self.s.cron_seen_at)
        self.assertFalse(DigiKeyPriceRun.objects.exists())
        self.s.auto_mode = "apply"
        self.s.save()
        call_command("dk_price_sync", "--if-due", stdout=mock.MagicMock())
        self.assertEqual(DigiKeyPriceRun.objects.count(), 1)
        self.assertFalse(svc.due())


class AdminTests(PriceBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.l = self.listing(self.product("AN-1", [(1, 10)], "USD"), "o1", [(1, 8)])

    def test_pages_and_flow(self):
        page = self.client.get("/admin/bots/digikeypricerun/compare/")
        self.assertContains(page, "AN-1")
        self.assertContains(page, "9.00")
        self.assertContains(page, "1 USD = <b>0.9</b>")
        page = self.client.get("/admin/bots/digikeypricerun/compare/?adjust_pct=10")
        self.assertContains(page, "9.90")
        self.assertContains(page, "змінено лише для цієї сторінки")
        self.assertContains(self.client.get("/admin/bots/digikeypricerun/compare/?adjust_pct=x"), "потрібне число")

        r = self.client.post("/admin/bots/digikeypricerun/compare/refresh/", {"query": ""})
        self.assertEqual(r.status_code, 302)
        self.l.refresh_from_db()
        self.assertIsNotNone(self.l.price_checked_at)

        r = self.client.post("/admin/bots/digikeypricerun/compare/listing/%d/" % self.l.pk,
                             {"price_adjust_pct": "-5", "price_adjust_fixed": "", "price_auto": "0"})
        self.assertEqual(r.json(), {"ok": True})
        self.l.refresh_from_db()
        self.assertEqual((self.l.price_adjust_pct, self.l.price_auto), (D("-5"), False))
        self.assertEqual(self.client.post("/admin/bots/digikeypricerun/compare/listing/%d/" % self.l.pk,
                                          {"price_adjust_pct": "abc"}).status_code, 400)

        r = self.client.post("/admin/bots/digikeypricerun/compare/apply/", {"ids": [self.l.pk], "adjust_pct": "0"})
        run = DigiKeyPriceRun.objects.get()
        self.assertRedirects(r, f"/admin/bots/digikeypricerun/{run.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(self.dk.puts[0][1]["prices"], [{"quantityBreak": 1, "price": 8.55}])   # 9 × 0,95
        page = self.client.get(f"/admin/bots/digikeypricerun/{run.pk}/change/")
        self.assertContains(page, "Оновлення цін DigiKey #%d" % run.pk)
        self.assertContains(page, "8.55")
        self.assertEqual(self.client.get(f"/admin/bots/digikeypricerun/{run.pk}/status/").json()["status"], "sent")
        self.client.post(f"/admin/bots/digikeypricerun/{run.pk}/verify/")
        run.refresh_from_db()
        self.assertEqual(run.status, DigiKeyPriceRun.VERIFIED)
        self.assertContains(self.client.get("/admin/bots/digikeypricerun/"), "Діє на DigiKey")

        r = self.client.post("/admin/bots/digikeypricerun/compare/save-rules/", {"adjust_pct": "3", "rounding": "x.90"})
        self.s.refresh_from_db()
        self.assertEqual((self.s.adjust_pct, self.s.rounding), (D("3"), "x.90"))
        self.assertContains(self.client.get("/admin/bots/digikeypricesettings/", follow=True), "Порівняти й оновити ціни")
        self.assertContains(self.client.get(f"/admin/bots/digikeylisting/{self.l.pk}/change/"), "Ціни з Minerva")

    def test_held_decisions_via_admin(self):
        self.s.auto_mode = "ask"
        self.s.notify_telegram = self.s.notify_email = False
        self.s.save()
        r = self.client.post("/admin/bots/digikeypricerun/compare/auto-now/", {"query": ""})
        run = DigiKeyPriceRun.objects.get()
        self.assertRedirects(r, f"/admin/bots/digikeypricerun/{run.pk}/change/", fetch_redirect_response=False)
        self.assertContains(self.client.get(f"/admin/bots/digikeypricerun/{run.pk}/change/"), "Відхилити")
        line = run.lines.get()
        self.client.post(f"/admin/bots/digikeypricerun/{run.pk}/decide/", {"ids": [line.pk], "decision": "confirm"})
        line.refresh_from_db()
        self.assertEqual(line.status, DigiKeyPriceChange.SENT)

    def test_view_only_user_cannot_apply(self):
        from django.contrib.auth.models import Permission
        u = User.objects.create_user("v", "v@example.com", "p", is_staff=True)
        u.user_permissions.add(Permission.objects.get(codename="view_digikeylisting"))
        u.profile.role = "admin"          # модуль bots відкрито; права Django — лише перегляд
        u.profile.save()
        self.client.force_login(u)
        self.assertEqual(self.client.get("/admin/bots/digikeypricerun/compare/").status_code, 200)
        self.assertEqual(self.client.post("/admin/bots/digikeypricerun/compare/apply/", {"ids": [self.l.pk]}).status_code, 403)
        self.assertFalse(DigiKeyPriceRun.objects.exists())


class ProductCardTests(PriceBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.user = User.objects.create_superuser("adm", "adm@example.com", "p")
        self.client.force_login(self.user)
        self.s.notify_telegram = self.s.notify_email = False
        self.s.save()
        self.p = self.product("AN-1", [(1, 10)])
        self.l = self.listing(self.p, "o1", [(1, 8)])

    def test_card_panel_and_push_now(self):
        page = self.client.get(f"/admin/inventory/product/{self.p.pk}/change/")
        self.assertContains(page, "Ціна на DigiKey")
        self.assertContains(page, "Зараз на DigiKey: <b>1: 8.00 EUR</b>")
        self.assertContains(page, "З цін Minerva стане: <b>1: 10.00 EUR</b> (+25 %)")
        self.assertContains(page, "Оновити на DigiKey зараз")
        self.assertContains(page, "dk_push_after_save")
        r = self.client.post("/admin/bots/digikeypricerun/compare/apply/", {"ids": [self.l.pk], "query": f"products={self.p.pk}"})
        run = DigiKeyPriceRun.objects.get()
        self.assertRedirects(r, f"/admin/bots/digikeypricerun/{run.pk}/change/", fetch_redirect_response=False)
        self.assertEqual(self.dk.puts[0][1]["prices"], [{"quantityBreak": 1, "price": 10.0}])
        self.assertEqual(self.dk.lists, 0)                                   # один товар — точковий GET
        self.assertContains(self.client.get(f"/admin/inventory/product/{self.p.pk}/change/"),
                            "На DigiKey ціни як у Minerva")

    def test_push_after_save(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory
        from bots.admin_prices import apply_after_save
        req = RequestFactory().post("/", {"dk_push_after_save": "1"})
        req.user, req.session = self.user, {}
        req._messages = FallbackStorage(req)
        Product.objects.filter(pk=self.p.pk).update(base_prices=[{"min_qty": 1, "unit_price": "11"}])
        apply_after_save(req, self.p)
        self.assertEqual(self.dk.puts[0][1]["prices"], [{"quantityBreak": 1, "price": 11.0}])
        apply_after_save(req, self.p)                                        # уже однакові — нічого
        self.assertEqual(len(self.dk.puts), 1)
        self.assertIn("нічого не надіслано", [str(m) for m in req._messages][-1])

    def test_bulk_actions_and_selected_page(self):
        other = self.listing(self.product("AN-2", [(1, 5)]), "o2", [(1, 4)])
        r = self.client.post("/admin/inventory/product/", {"action": "action_dk_prices",
                                                           "_selected_action": [self.p.pk]})
        self.assertEqual(r["Location"], f"/admin/bots/digikeypricerun/compare/?products={self.p.pk}")
        page = self.client.get(r["Location"])
        self.assertContains(page, "Показано лише вибрані товари (1)")
        self.assertContains(page, 'value="%d" class="dp-chk" checked' % self.l.pk)
        self.assertNotContains(page, "AN-2")
        self.assertEqual((self.dk.singles, self.dk.lists), (1, 0))           # свіжі ціни — лише вибраного
        r = self.client.post("/admin/bots/digikeylisting/", {"action": "action_dk_from_minerva",
                                                             "_selected_action": [other.pk]})
        self.assertEqual(r["Location"], f"/admin/bots/digikeypricerun/compare/?ids={other.pk}")

    def test_full_page_reads_digikey_when_stale(self):
        self.client.get("/admin/bots/digikeypricerun/compare/")
        self.assertEqual(self.dk.singles, 1)                                 # ще не читались → прочитано
        self.client.get("/admin/bots/digikeypricerun/compare/")
        self.assertEqual(self.dk.singles, 1)                                 # свіжі — без DigiKey
        DigiKeyListing.objects.update(price_checked_at=timezone.now() - timedelta(hours=2))
        with mock.patch.object(svc, "_fetch_one", side_effect=Exception("down")):
            page = self.client.get("/admin/bots/digikeypricerun/compare/", follow=True)
        self.assertContains(page, "не прочитано (down)")
        self.client.get("/admin/bots/digikeypricerun/compare/")
        self.assertEqual(self.dk.singles, 1)                                 # після збою 5 хв не пробує
