"""💶 Порівняння пропозицій запиту: кількості колонками, разові витрати, що входить, неповні, курс, 🧠 усі разом.

python manage.py test suppliers.tests_rfq_compare --settings=tabele.settings_test
"""
import io
import json
from decimal import Decimal
from unittest import mock

from django.core.files.base import ContentFile
from django.urls import reverse
from django.utils import timezone

from inventory.models import Supplier
from offers.pricing import home_currency
from suppliers.models import SupplierReplyFile, SupplierRequest, SupplierRequestRecipient as R
from suppliers.services import ai, rfq, rfq_compare
from suppliers.tests import ME, Base


class CompareTests(Base):
    def setUp(self):
        super().setUp()
        from shop.models import ShopSettings
        self.home = home_currency()
        ss = ShopSettings.get()
        ss.fx_rates = {"USD": "0.8"} if self.home != "USD" else {"EUR": "1.25"}
        ss.save()
        self.cur = "USD" if self.home != "USD" else "EUR"
        self.req = SupplierRequest.objects.create(title="PCBA TM270402", template="pcba", quantities="5; 2500",
                                                  owner=self.user, account=self.account,
                                                  status=SupplierRequest.ANSWERED)
        self.full = R.objects.create(request=self.req, supplier=self.supplier, email="a@pcbway.com", state=R.QUOTED,
                                     currency=self.cur, includes=["pcb", "assembly", "components"],
                                     tiers=[{"qty": 5, "unit_price": "246.2"}, {"qty": 2500, "unit_price": "15.362"}])
        self.bare = R.objects.create(
            request=self.req, supplier=Supplier.objects.create(name="Victory", email="j@victory.com"),
            email="j@victory.com", state=R.QUOTED, currency=self.cur, includes=["pcb"],
            tiers=[{"qty": 5, "unit_price": "0.574"}, {"qty": 2500, "unit_price": "0.455"}],
            one_time=[{"name": "Test jig", "amount": "200", "qty": None}])
        self.half = R.objects.create(
            request=self.req, supplier=Supplier.objects.create(name="Half", email="h@half.com"),
            email="h@half.com", state=R.QUOTED, currency=self.cur, includes=["pcb", "assembly"],
            tiers=[{"qty": 5, "unit_price": "200"}], one_time=[{"name": "Stencil", "amount": "50", "qty": 5}])
        self.silent = R.objects.create(
            request=self.req, supplier=Supplier.objects.create(name="Waiting", email="w@w.com"),
            email="w@w.com", state=R.SENT)

    def row(self, t, r):
        return next(x for x in t["rows"] if x["recipient"].pk == r.pk)

    def test_quantities_one_time_scope_and_best(self):
        t = rfq_compare.table(self.req)
        self.assertEqual(t["qtys"], [5, 2500])
        full, bare, half = self.row(t, self.full), self.row(t, self.bare), self.row(t, self.half)
        # голі плати на запит PCBA — неповна, у 🥇 не бере участі
        self.assertFalse(bare["comparable"])
        self.assertIn("у ціні немає: монтаж", bare["gaps"])
        c = bare["cells"][0]
        self.assertEqual(c["total"], Decimal("202.87"))                       # 0.574 × 5 + 200 разово
        self.assertEqual(c["one_time"], Decimal("200"))
        self.assertNotIn("best", c)
        # ціни лише на 5 шт — порівнюється, але «неповна»
        self.assertTrue(half["comparable"])
        self.assertTrue(half["cells"][1]["missing"])
        self.assertIn("немає ціни на 2 500 шт", half["gaps"][0])
        self.assertEqual(half["cells"][0]["total"], Decimal("1050.00"))      # 200 × 5 + 50 (лише для 5 шт)
        # 5 шт: Half 1050 < Full 1231 → 🥇 у Half, Full +17 %; 2500 шт: лише Full
        self.assertTrue(half["cells"][0]["best"])
        self.assertEqual((full["cells"][0]["over"], full["cells"][0]["over_text"]), (17, "+17 %"))
        self.assertEqual(rfq_compare.over_text(42723), "×428")
        self.assertIn(rfq_compare.over_text(350), ("×4,5", "×4.5"))
        self.assertTrue(full["cells"][1]["best"])
        self.assertEqual(full["gaps"], [])
        if self.cur == "USD":
            self.assertEqual(full["cells"][0]["total_home"], Decimal("984.80"))  # 1231 USD × 0.8
        # повні — першими, чекаємо — наприкінці
        order = [x["recipient"].pk for x in t["rows"]]
        self.assertLess(order.index(self.half.pk), order.index(self.bare.pk))
        self.assertEqual(order[-1], self.silent.pk)
        self.assertEqual(t["counts"]["comparable"], 2)
        self.assertEqual(t["counts"]["incomplete"], 2)
        self.assertEqual(t["counts"]["waiting"], 1)

    def test_near_quantity_and_unknown_scope(self):
        self.full.tiers = [{"qty": 5, "unit_price": "246.2"}, {"qty": 3000, "unit_price": "15"}]
        self.full.includes = []
        self.full.save()
        row = self.row(rfq_compare.table(self.req), self.full)
        self.assertFalse(row["cells"][1]["exact"])                             # 3000 замість 2500 — «≈»
        self.assertIn("ціна на 3000 шт замість 2500", row["notes"])
        # не сказано, що входить, а просили монтаж — не знаємо, чи це те саме: неповна, у 🥇 не бере участі
        self.assertIn("не сказано, що входить у ціну (просили: 🔧 монтаж)", row["gaps"])
        self.assertFalse(row["comparable"])
        self.assertNotIn("best", row["cells"][0])
        self.full.tiers = [{"qty": 5, "unit_price": "246.2"}, {"qty": 10000, "unit_price": "13"}]
        self.full.save()
        self.assertTrue(self.row(rfq_compare.table(self.req), self.full)["cells"][1]["missing"])   # надто далеко

    def test_rate_from_ecb_when_minerva_has_none(self):
        from shop.models import ShopSettings
        ss = ShopSettings.get()
        ss.fx_rates = {}
        ss.save()
        row = self.row(rfq_compare.table(self.req), self.full)
        self.assertFalse(row["comparable"])
        self.assertIn("немає курсу", row["gaps"][-1])
        rfq_compare.ONLINE_FX = True
        ecb = {"date": "2026-11-16", "rates": {"EUR": "1", "USD": "1.25"}}
        ecb["rates"].setdefault(self.home, "2")
        with mock.patch("core.fx_online.latest", return_value=ecb):
            t = rfq_compare.table(self.req)
        self.assertTrue(self.row(t, self.full)["comparable"])
        self.assertIn("ЄЦБ на 2026-11-16", t["fx"][0]["source"])

    def test_work_page_shows_matrix_gaps_and_medals(self):
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("2500 шт", page)
        self.assertIn("🥇", page)
        self.assertIn("неповна:", page)
        self.assertIn("у ціні немає: монтаж", page)
        self.assertIn("— немає ціни", page)
        self.assertIn("Test jig", page)
        self.assertIn("⏳ чекаємо відповідь", page)

    def test_manual_quote_with_quantities_one_time_and_scope(self):
        self.client.post(reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk]), {
            "do": "quote", "rid": self.silent.pk, "tiers_text": "5: 100; 2500: 9,5 / 21 дн",
            "one_time_text": "Stencil 30; NRE 150 @2500", "includes_set": "1", "includes": ["pcb", "assembly"],
            "currency": self.cur, "price": "", "total": ""})
        self.silent.refresh_from_db()
        self.assertEqual(self.silent.state, R.QUOTED)
        self.assertEqual(self.silent.tiers[1], {"qty": 2500, "unit_price": "9.5000", "lead_days": 21})
        self.assertEqual(self.silent.one_time[1], {"name": "NRE", "amount": "150.00", "qty": 2500})
        self.assertEqual(self.silent.includes, ["pcb", "assembly"])
        self.assertEqual(self.silent.price, Decimal("100"))
        cell = self.row(rfq_compare.table(self.req), self.silent)["cells"][1]
        self.assertEqual(cell["total"], Decimal("23930.00"))                   # 9.5 × 2500 + 30 + 150
        res = self.client.post(reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk]), {
            "do": "quote", "rid": self.silent.pk, "tiers_text": "сто штук", "currency": self.cur}, follow=True)
        self.assertContains(res, "кількість: ціна")

    def test_stale_advice(self):
        self.req.analysis = {"summary": "Жодної ціни", "at": (timezone.now() - timezone.timedelta(days=1)).isoformat()}
        self.req.save()
        R.objects.filter(pk=self.full.pk).update(replied_at=timezone.now())
        self.assertTrue(rfq_compare.stale(self.req))
        self.req.analysis = {"summary": "ok", "fp": rfq_compare.fingerprint(self.req)}
        self.req.save()
        self.assertFalse(rfq_compare.stale(self.req))
        R.objects.filter(pk=self.half.pk).update(tiers=[{"qty": 5, "unit_price": "190"}])
        self.assertTrue(rfq_compare.stale(self.req))
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("Порада складена до теперішніх цін", page)


class CountTests(Base):
    """⚖ «Рахувати в сумі»: вимкнена складова віднімається від ціни за шт. (як назвав постачальник), разові й
    доставка додаються чи ні; «Freight cost» серед разових — доставка, а не NRE."""

    def setUp(self):
        super().setUp()
        self.home = home_currency()
        self.req = SupplierRequest.objects.create(title="PCBA", template="pcba", quantities="100", owner=self.user,
                                                  account=self.account, status=SupplierRequest.ANSWERED)
        self.a = R.objects.create(
            request=self.req, supplier=self.supplier, email="a@pcbway.com", state=R.QUOTED, currency=self.home,
            includes=["pcb", "assembly", "components"],
            tiers=[{"qty": 100, "unit_price": "10", "total": "1130", "total_ot": True,
                    "parts": {"pcb": "2", "assembly": "3", "components": "5"}}],
            one_time=[{"name": "Stencil", "amount": "50", "qty": None},
                      {"name": "Freight cost", "amount": "80", "qty": None}])
        self.b = R.objects.create(
            request=self.req, supplier=Supplier.objects.create(name="JY", email="c@jy.com"), email="c@jy.com",
            state=R.QUOTED, currency=self.home, includes=["pcb", "assembly", "components"],
            tiers=[{"qty": 100, "unit_price": "9"}], shipping=Decimal("30"))

    def cells(self, count=None):
        t = rfq_compare.table(self.req, count=count)
        return t, {x["recipient"].pk: x["cells"][0] for x in t["rows"]}

    def test_default_counts_nre_and_not_shipping_freight_is_shipping(self):
        t, c = self.cells()
        a, b = c[self.a.pk], c[self.b.pk]
        self.assertEqual((a["total"], b["total"]), (Decimal("1050.00"), Decimal("900.00")))   # Freight 80 — не NRE
        self.assertFalse(a["sum_mismatch"])               # сума постачальника (з Freight) — з його «як є»
        ship = {f["key"]: f for f in a["flags"]}["shipping"]
        self.assertEqual((ship["state"], ship["on"]), ("y", False))
        self.assertIn("Freight cost", ship["title"])
        self.assertTrue(b["best"])
        self.assertTrue(t["count_default"])
        self.assertEqual(t["sum_sub"], "з разовими")

    def test_with_shipping_and_without_components(self):
        t, c = self.cells("pcb,assembly,components,one_time,shipping")
        self.assertEqual((c[self.a.pk]["total"], c[self.b.pk]["total"]), (Decimal("1130.00"), Decimal("930.00")))
        self.assertEqual(c[self.b.pk]["ship_in_text"], "30,00")
        self.assertEqual(t["sum_sub"], "з разовими і доставкою")
        t, c = self.cells(["pcb", "assembly", "one_time"])
        a, b = c[self.a.pk], c[self.b.pk]
        self.assertEqual((a["unit"], a["total"]), (Decimal("5"), Decimal("550.00")))        # 10 − 🔩 5
        self.assertIn("−🔩 5", a["minus_text"])
        self.assertEqual(b["cant"], ["компоненти"])        # скільки компоненти — JY не назвав
        self.assertFalse(b["ok"])
        self.assertTrue(a["best"])
        row_b = next(x for x in t["rows"] if x["recipient"].pk == self.b.pk)
        self.assertIn("не можна відняти: компоненти — постачальник не назвав, скільки це", row_b["gaps"])
        self.assertEqual(t["unit_sub"], "без разових, без: компоненти")
        _t, c = self.cells(["pcb", "assembly", "components"])
        self.assertEqual(c[self.a.pk]["total"], Decimal("1000.00"))                       # без разових

    def test_what_was_asked_only_from_what_counts(self):
        R.objects.filter(pk=self.b.pk).update(includes=["pcb"])
        _t, c = self.cells()
        self.assertEqual(c[self.b.pk]["lacks"], ["монтаж"])
        _t, c = self.cells(["pcb", "components", "one_time"])     # монтаж не рахуємо — і не вимагаємо
        self.assertEqual(c[self.b.pk]["lacks"], [])

    def test_clean_count(self):
        self.assertEqual(rfq_compare.clean_count(None), rfq_compare.COUNT_DEFAULT)
        self.assertEqual(rfq_compare.clean_count("shipping,pcb,evil"), ("pcb", "shipping"))
        self.assertEqual(rfq_compare.clean_count("none"), ())
        self.assertEqual(rfq_compare.clean_count("zzz"), rfq_compare.COUNT_DEFAULT)

    def test_breakdown_does_not_add_shipping_twice(self):
        t = rfq_compare.table(self.req, count="pcb,assembly,components,one_time,shipping")
        b = next(x for x in rfq_compare.breakdown(t) if x["rid"] == self.b.pk)["cells"][0]
        self.assertEqual(b["total"]["v"], b["total_ship"]["v"])
        t = rfq_compare.table(self.req)
        a = next(x for x in rfq_compare.breakdown(t) if x["rid"] == self.a.pk)["cells"][0]
        self.assertEqual((a["total"]["v"], a["total_ship"]["v"]), (1050.0, 1130.0))
        self.assertEqual([x["name"] for x in a["one_time"]], ["Stencil"])

    def test_work_page_takes_the_choice_from_cookie_and_link(self):
        url = reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])
        page = self.client.get(url).content.decode()
        self.assertIn("⚖ Рахувати в сумі:", page)
        self.assertNotIn("↺ Як завжди", page)
        self.client.cookies["rw_sum"] = "pcb,assembly,one_time"
        page = self.client.get(url).content.decode()
        self.assertIn('data-rw-count-set="pcb,assembly,one_time"', page)
        self.assertIn("↺ Як завжди", page)
        self.assertIn("rw-chip y off", page)                        # 🔩 у рядку — закреслено
        self.assertIn("550,00", page)
        page = self.client.get(url + "?sum=pcb,assembly,components,one_time,shipping").content.decode()
        self.assertIn('data-rw-count-set="pcb,assembly,components,one_time,shipping"', page)
        self.assertIn("1\xa0130,00", page)


class QuoteReadingTests(Base):
    def setUp(self):
        super().setUp()
        from email_assistant.models import EmailThread
        self.req = SupplierRequest.objects.create(title="PCBA", template="pcba", quantities="5; 2500", owner=self.user,
                                                  account=self.account, status=SupplierRequest.SENT)
        self.rows = []
        for n, (name, email) in enumerate([("PCBWay", "sales@pcbway.com"), ("JY", "charles@jypcba.com")]):
            s = self.supplier if n == 0 else Supplier.objects.create(name=name, email=email)
            th = EmailThread.objects.create(account=self.account, subject=f"[{self.req.code}] PCBA {n}")
            sent = self.mail(frm=ME, to=[email], folder="sent", subject=th.subject, thread=th, body="Please quote")
            r = R.objects.create(request=self.req, supplier=s, email=email, state=R.REPLIED, thread=th,
                                 sent_message=sent, replied_at=timezone.now())
            self.mail(frm=email, subject=f"Re: {th.subject}", thread=th, body="Please find our quotation attached.")
            self.rows.append(r)

    def xlsx(self, r):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Qty", "Unit price USD", "Stencil"])
        ws.append([5, 246.2, 30])
        ws.append([2500, 15.362, ""])
        buf = io.BytesIO()
        wb.save(buf)
        f = SupplierReplyFile(recipient=r, name="Quotation.xlsx", size=len(buf.getvalue()), sha256="x" * 64)
        f.file.save("Quotation.xlsx", ContentFile(buf.getvalue()), save=False)
        f.save()

    def test_reply_file_xlsx_read_and_new_fields_applied(self):
        r = self.rows[0]
        self.xlsx(r)
        answer = json.dumps({"currency": "USD", "tiers": [{"qty": 5, "unit_price": 246.2, "lead_days": 10},
                                                          {"qty": 2500, "unit_price": 15.362, "lead_days": 25}],
                             "one_time": [{"name": "Stencil", "amount": 30}], "includes": ["pcb", "assembly", "x"],
                             "summary": "PCBA"})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.002"))) as call:
            data = ai.quote(r, user=self.user)
        sent = call.call_args[0][1]
        self.assertIn("Quotation.xlsx", sent)
        self.assertIn("246.2", sent)                                            # таблицю xlsx прочитано
        self.assertIn("БЕЗ разових", call.call_args[0][0])
        self.assertEqual(data["v"], ai.QUOTE_VERSION)
        self.assertEqual(data["includes"], ["pcb", "assembly"])
        rfq.apply_ai(r, data)
        r.refresh_from_db()
        self.assertEqual((r.state, r.includes, r.one_time[0]["amount"]), (R.QUOTED, ["pcb", "assembly"], "30.00"))
        self.assertEqual(r.tiers[1]["lead_days"], 25)
        self.assertEqual(r.total, Decimal("1261.00"))                           # 246.2 × 5 + 30

    def test_parse_all_replies_at_once(self):
        answers = iter([
            (json.dumps({"currency": "USD", "tiers": [{"qty": 5, "unit_price": 3}], "summary": "a"}), Decimal("0.001")),
            (json.dumps({"tiers": [], "questions": ["Gerber?"], "summary": "питають Gerber"}), Decimal("0.001"))])
        act = reverse("admin:suppliers_supplierrequest_action", args=[self.req.pk])
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("Розібрати відповіді (2)", page)
        self.assertIn("ціни ще не розібрано", page)
        with mock.patch.object(ai, "call", side_effect=lambda *a, **k: next(answers)):
            job = self.client.post(act, {"do": "ai_quote_all"}).json()["job"]
        from suppliers.services import jobs
        state = jobs.state(job, self.user)
        self.assertEqual(state["status"], "done")
        self.assertIn("Розібрано 2: з цінами 1, без цін 1", state["result"]["message"])
        a, b = [R.objects.get(pk=r.pk) for r in self.rows]
        self.assertEqual((a.state, b.state), (R.QUOTED, R.REPLIED))
        self.assertEqual(self.client.post(act, {"do": "ai_quote_all"}).status_code, 400)   # уже все прочитано
        page = self.client.get(reverse("admin:suppliers_supplierrequest_work", args=[self.req.pk])).content.decode()
        self.assertIn("цін у відповіді немає", page)
        self.assertNotIn("Розібрати відповіді (", page)

    def test_compare_prompt_marks_incomplete_and_unparsed(self):
        R.objects.filter(pk=self.rows[0].pk).update(state=R.QUOTED, currency="", includes=["pcb"],
                                                    tiers=[{"qty": 5, "unit_price": "3"}])
        answer = json.dumps({"summary": "s", "pick": "", "reasons": [], "risks": [], "ask": []})
        with mock.patch.object(ai, "call", return_value=(answer, Decimal("0.001"))) as call:
            ai.compare(self.req, user=self.user)
        text = call.call_args[0][1]
        self.assertIn("⚠️ НЕПОВНА", text)
        self.assertIn("у ціні немає: монтаж", text)
        self.assertIn("ціни ще не розібрано", text)
        self.assertIn("Має бути в ціні: 🔧 монтаж", text)
