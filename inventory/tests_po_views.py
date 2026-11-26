"""🔄 Швидкі вигляди списку закупівель — чипи над списком (inventory/po_views.py), вибір людини
(core/list_views.py, UserProfile.ui_prefs["list_views"]).

python manage.py test inventory.tests_po_views --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core import list_views
from inventory import po_views
from inventory.models import Product, PurchaseOrder, PurchaseOrderLine, Supplier

URL = reverse("admin:inventory_purchaseorder_changelist")


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.user = User.objects.create_superuser("slava", "s@x.y", "p")
        self.client.force_login(self.user)
        self.acme = Supplier.objects.create(name="ACME")
        self.jlc = Supplier.objects.create(name="JLCPCB")
        self.pos = {}
        for code, status, sup in (("PO-D", "draft", self.acme), ("PO-O1", "ordered", self.acme),
                                  ("PO-O2", "ordered", self.jlc), ("PO-P", "partial", self.jlc),
                                  ("PO-R1", "received", self.jlc), ("PO-R2", "received", self.acme),
                                  ("PO-R3", "received", self.jlc), ("PO-C", "cancelled", self.acme)):
            self.pos[code] = PurchaseOrder.objects.create(code=code, status=status, supplier=sup)

    def shown(self, params):
        r = self.client.get(URL, params)
        self.assertEqual(r.status_code, 200)
        return sorted(po.code for po in r.context["cl"].result_list), r

    def chips(self, r):
        return {c["key"]: c for c in r.context["cl"].po_chips}


class ViewTests(Base):
    def test_each_view_shows_its_statuses(self):
        self.assertEqual(self.shown({"view": "active"})[0], ["PO-D", "PO-O1", "PO-O2", "PO-P"])
        self.assertEqual(self.shown({"view": "ordered"})[0], ["PO-O1", "PO-O2"])
        self.assertEqual(self.shown({"view": "partial"})[0], ["PO-P"])
        self.assertEqual(self.shown({"view": "received"})[0], ["PO-R1", "PO-R2", "PO-R3"])
        self.assertEqual(self.shown({"view": "cancelled"})[0], ["PO-C"])
        self.assertEqual(self.shown({"view": "draft"})[0], ["PO-D"])
        self.assertEqual(len(self.shown({"view": "all"})[0]), 8)

    def test_chip_counts_and_links(self):
        _codes, r = self.shown({"view": "ordered"})
        chips = self.chips(r)
        self.assertEqual({k: c["count"] for k, c in chips.items()},
                         {"active": 4, "draft": 1, "ordered": 2, "partial": 1, "received": 3, "cancelled": 1, "all": 8})
        self.assertEqual([k for k, c in chips.items() if c["on"]], ["ordered"])
        self.assertEqual(chips["received"]["href"], "?view=received")
        html = r.content.decode()
        self.assertIn('class="po-chips"', html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("🔄 В роботі", html)
        # «Статус» праворуч більше немає — його замінили чипи
        self.assertNotIn("?status__exact=", html)

    def test_counts_follow_search_and_filters(self):
        _codes, r = self.shown({"view": "active", "supplier__id__exact": self.jlc.pk})
        self.assertEqual(_codes, ["PO-O2", "PO-P"])
        chips = self.chips(r)
        self.assertEqual((chips["active"]["count"], chips["received"]["count"], chips["all"]["count"]), (2, 2, 4))
        # чип лишає фільтр постачальника, міняє лише вигляд
        self.assertEqual(chips["received"]["href"], f"?supplier__id__exact={self.jlc.pk}&view=received")

    def test_search_over_lines_counts_each_order_once(self):
        p = Product.objects.create(sku="TR-1", name="PCB")
        for po in (self.pos["PO-O1"], self.pos["PO-R1"]):
            PurchaseOrderLine.objects.create(purchase_order=po, product=p, qty_ordered=1)
            PurchaseOrderLine.objects.create(purchase_order=po, product=p, qty_ordered=2)
        codes, r = self.shown({"view": "all", "q": "TR-1"})
        self.assertEqual(codes, ["PO-O1", "PO-R1"])
        chips = self.chips(r)
        self.assertEqual((chips["ordered"]["count"], chips["received"]["count"], chips["all"]["count"]), (1, 1, 2))

    def test_view_is_not_a_django_filter(self):
        """«✖ Скинути фільтри» праворуч вигляд не скидає; сортування й сторінки його зберігають."""
        _codes, r = self.shown({"view": "received", "supplier__id__exact": self.acme.pk})
        cl = r.context["cl"]
        self.assertIn("view=received", cl.clear_all_filters_qs)
        self.assertNotIn("supplier", cl.clear_all_filters_qs)
        self.assertIn("view=received", cl.get_query_string({"o": "1"}))
        _codes, r = self.shown({"view": "received"})
        self.assertFalse(r.context["cl"].has_active_filters)

    def test_old_status_links_still_work(self):
        codes, r = self.shown({"status__exact": "partial"})
        self.assertEqual(codes, ["PO-P"])
        chips = self.chips(r)
        self.assertTrue(chips["partial"]["on"])
        self.assertEqual(chips["active"]["count"], 4)              # лічильники — не лише цього статусу
        self.assertEqual(chips["ordered"]["href"], "?view=ordered")  # чип прибирає старий параметр

    def test_draft_chip_only_when_there_are_drafts(self):
        self.pos["PO-D"].delete()
        self.assertNotIn("draft", self.chips(self.shown({"view": "all"})[1]))
        self.assertIn("draft", self.chips(self.shown({"view": "draft"})[1]))   # вибраний — видно й з 0

    def test_unknown_view_shows_everything(self):
        codes, r = self.shown({"view": "nonsense"})
        self.assertEqual(len(codes), 8)
        self.assertTrue(self.chips(r)["all"]["on"])


class RememberTests(Base):
    def test_first_visit_opens_in_work(self):
        r = self.client.get(URL)
        self.assertRedirects(r, URL + "?view=active", fetch_redirect_response=False)

    def test_choice_is_remembered_per_person(self):
        self.shown({"view": "received"})
        self.assertEqual(list_views.saved(User.objects.get(pk=self.user.pk), po_views.LIST_KEY), "received")
        self.assertRedirects(self.client.get(URL), URL + "?view=received", fetch_redirect_response=False)
        other = User.objects.create_superuser("anna", "a@x.y", "p")
        self.client.force_login(other)
        self.assertRedirects(self.client.get(URL), URL + "?view=active", fetch_redirect_response=False)

    def test_all_is_remembered_too(self):
        self.shown({"view": "all"})
        r = self.client.get(URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["cl"].result_list), 8)
        self.assertTrue(self.chips(r)["all"]["on"])

    def test_explicit_params_win_and_are_not_saved(self):
        self.shown({"view": "cancelled"})
        codes, _r = self.shown({"supplier__id__exact": self.acme.pk})   # посилання з картки постачальника
        self.assertEqual(codes, ["PO-C", "PO-D", "PO-O1", "PO-R2"])
        self.assertEqual(list_views.saved(User.objects.get(pk=self.user.pk), po_views.LIST_KEY), "cancelled")
        self.assertEqual(self.client.get(URL, {"_popup": "1"}).status_code, 200)   # вибір у вікні — без переходу

    def test_other_ui_prefs_stay(self):
        p = self.user.profile
        p.ui_prefs = {"theme": "light", "menu": {"mode": "full"}}
        p.save(update_fields=["ui_prefs"])
        self.shown({"view": "partial"})
        p.refresh_from_db()
        self.assertEqual(p.ui_prefs, {"theme": "light", "menu": {"mode": "full"},
                                      "list_views": {po_views.LIST_KEY: "partial"}})

    def test_bad_saved_value_falls_back(self):
        p = self.user.profile
        p.ui_prefs = {"list_views": {po_views.LIST_KEY: "<script>"}}
        p.save(update_fields=["ui_prefs"])
        self.assertRedirects(self.client.get(URL), URL + "?view=active", fetch_redirect_response=False)

    def test_list_action_post_is_not_redirected(self):
        r = self.client.post(URL, {"action": "detect_ordered", "_selected_action": [self.pos["PO-O1"].pk]})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("view=", r["Location"])


class ListViewsTests(TestCase):
    def test_remember_writes_only_on_change(self):
        user = User.objects.create_user("u", "u@x.y", "p")
        self.assertIsNone(list_views.saved(user, "x.y"))
        self.assertTrue(list_views.remember(user, "x.y", "a"))
        self.assertFalse(list_views.remember(user, "x.y", "a"))
        self.assertEqual(list_views.saved(user, "x.y"), "a")
        self.assertTrue(list_views.remember(user, "x.y", None))
        self.assertIsNone(list_views.saved(user, "x.y"))
