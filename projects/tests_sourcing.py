"""📨 Запити постачальникам у проєкті: кому писали, хто що запропонував, кого обрали на яку партію, кого не взяли.
python manage.py test projects --settings=tabele.settings_test"""
import json
from decimal import Decimal

from django.urls import reverse

from inventory.models import Supplier
from projects import parties, portfolio, sourcing
from projects.models import ProjectParty
from projects.tests_portfolio import PortfolioBase
from suppliers.models import SupplierRequest, SupplierRequestRecipient as R


class SourcingBase(PortfolioBase):
    def setUp(self):
        super().setUp()
        self.req = SupplierRequest.objects.create(title="PCBA LTE-плата", template="pcba", project=self.project,
                                                  quantities="500; 1500", status=SupplierRequest.ANSWERED)
        self.sup = {}
        for name, state, price in (("HongRong Electronics", R.QUOTED, "12.40"), ("Toptek PCB", R.QUOTED, "13.10"),
                                   ("Fab Nord", R.DECLINED, None), ("Quiet Boards", R.SENT, None)):
            s = Supplier.objects.create(name=name, email=f"{name.split()[0].lower()}@x.example")
            self.sup[name] = R.objects.create(request=self.req, supplier=s, email=s.email, state=state,
                                              price=Decimal(price) if price else None,
                                              currency="USD" if price else "", lead_days=21 if price else None)

    def decide(self, name, action, text=""):
        return self.client.post(reverse("projects:sourcing", args=[self.project.pk]),
                                json.dumps({"recipient": self.sup[name].pk, "action": action, "text": text}),
                                content_type="application/json")


class SourcingTests(SourcingBase):
    def test_context_shows_who_was_asked_and_what_they_offered(self):
        ctx = sourcing.context(self.project, self.user)
        q = ctx["rows"][0]
        self.assertEqual((q["code"], q["title"]), (self.req.code, "PCBA LTE-плата"))
        names = [r["supplier"] for r in q["recipients"]]
        self.assertEqual(names[:2], ["HongRong Electronics", "Toptek PCB"])          # пропозиції — першими
        hong = q["recipients"][0]
        self.assertEqual(hong["outcome"], "quoted")
        self.assertIn("12.40 USD / шт", hong["price"])
        self.assertIn("21 дн.", hong["price"])
        self.assertEqual({r["supplier"]: r["outcome"] for r in q["recipients"]}["Fab Nord"], "declined")
        self.assertTrue(ctx["can_decide"])
        self.assertIn(f"?project={self.project.pk}", ctx["new_url"])

    def test_two_suppliers_for_two_batches_and_a_rejection_with_reason(self):
        self.assertEqual(self.decide("HongRong Electronics", "award", "партія 1 · 500 шт").status_code, 200)
        self.assertEqual(self.decide("Toptek PCB", "award", "партія 2 · 1500 шт").status_code, 200)
        r = self.decide("Quiet Boards", "reject", "не відповіли вчасно")
        self.assertEqual(r.status_code, 200)
        self.assertIn("✖ Не беремо Quiet Boards", r.json()["html"])
        rows = {x.supplier.name: x for x in R.objects.filter(request=self.req)}
        self.assertTrue(rows["HongRong Electronics"].chosen and rows["Toptek PCB"].chosen)   # обидва — на свої партії
        self.assertEqual(rows["Toptek PCB"].award, "партія 2 · 1500 шт")
        self.assertEqual(rows["Quiet Boards"].reject_reason, "не відповіли вчасно")
        self.decide("Quiet Boards", "reset")
        self.assertEqual(R.objects.get(pk=self.sup["Quiet Boards"].pk).reject_reason, "")
        self.assertIn("✅ PCBA LTE-плата · партія 1 · 500 шт",
                      sourcing.line(R.objects.get(pk=self.sup["HongRong Electronics"].pk), self.req))

    def test_rights_and_other_projects(self):
        from projects.models import Project
        other = Project.objects.create(name="Інший")
        r = self.client.post(reverse("projects:sourcing", args=[other.pk]),
                             json.dumps({"recipient": self.sup["Toptek PCB"].pk, "action": "award"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)                                       # отримувач чужого проєкту
        u = self.viewer("pm", role="manager", modules=["projects"])
        self.client.force_login(u)
        self.assertEqual(self.decide("Toptek PCB", "award").status_code, 403)      # без права на запити

    def test_diagram_shows_requested_chosen_and_rejected(self):
        self.decide("HongRong Electronics", "award", "партія 1")
        self.decide("Quiet Boards", "reject", "ціна")
        parties.save(self.project, {"role": ProjectParty.WE, "name": "Sevskiy"})
        ctx = parties.context(self.project, True)
        svg = ctx["svg"]
        self.assertIn("📨 Запит постачальнику", svg)                               # лише із запиту — окремий вузол
        self.assertIn("HongRong Electronics", svg)
        self.assertIn("pjy-cand", svg)                                             # Toptek — кандидат пунктиром
        self.assertIn("pjy-off", svg)                                              # Quiet / Fab — бліді
        self.assertNotIn('data-party="r', svg)                                     # вузли запитів не редагуються як сторони
        added = parties.suggest(self.project)
        self.assertGreaterEqual(added, 1)
        hong = self.project.parties.get(name="HongRong Electronics")
        self.assertEqual((hong.role, hong.what), (ProjectParty.MANUFACTURER, "PCBA LTE-плата · партія 1"))

    def test_portfolio_map_edges(self):
        self.decide("HongRong Electronics", "award", "партія 1")
        rows = portfolio.load([self.project], self.user)
        rel = portfolio.relations(rows, [])
        kinds = {e["b"]: e["kind"] for e in rel["edges"] if e["a"] == f"p{self.project.pk}"}
        sid = {n: f"s{r.supplier_id}" for n, r in self.sup.items()}
        self.assertEqual(kinds[sid["HongRong Electronics"]], "supply")
        self.assertEqual(kinds[sid["Toptek PCB"]], "plan")
        self.assertEqual(kinds[sid["Fab Nord"]], "no")

    def test_ai_facts_mention_the_decisions(self):
        self.decide("HongRong Electronics", "award", "партія 1")
        text = sourcing.facts_text(self.project)
        self.assertIn("ЗАПИТИ ПОСТАЧАЛЬНИКАМ", text)
        self.assertIn("HongRong Electronics — ✅ PCBA LTE-плата · партія 1", text)


class ChooseStillSingleTests(SourcingBase):
    def test_choose_on_the_request_page_keeps_one_winner(self):
        from suppliers.services import rfq
        rfq.award(self.sup["HongRong Electronics"], "партія 1")
        rfq.choose(R.objects.get(pk=self.sup["Toptek PCB"].pk))
        self.assertEqual(list(R.objects.filter(request=self.req, chosen=True).values_list("supplier__name", flat=True)),
                         ["Toptek PCB"])
