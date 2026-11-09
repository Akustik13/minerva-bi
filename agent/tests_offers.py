"""📝 Агент готує чернетку Angebot (agent/offers_tool.py): клієнт, проєкт, мова, позиції — артикули й кількості
ступенями; ціни й строки рахує Minerva. Застосовує людина (✓) — чернетка в легкій формі «⚡».

python manage.py test agent.tests_offers --settings=tabele.settings_test
"""
from django.contrib.auth.models import User

from agent import decide, tools
from agent.models import AgentProposal
from agent.tests_tasks import Base
from crm.models import Customer
from offers.models import Offer


class OfferToolTests(Base):
    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(name="Max Mustermann", company="Muster GmbH",
                                                email="max@muster.example")

    def ctx(self):
        return tools.Context(self.task("Підготуй Angebot за запитом клієнта.", products=[]))

    def call(self, ctx, ok=True, **args):
        base = {"customer": "Muster GmbH", "language": "de",
                "lines": [{"sku": "an220213-01c", "qty": "35; 100"},
                          {"qty": "500", "description": "Kabel nach Zeichnung, 150 mm"}],
                "evidence": "Bitte um Angebot für 35 und 100 Stück", "source": "Anfrage 5G Antenne, 03.11.2026"}
        base.update(args)
        out, err = tools.run(ctx, "propose_offer", base)
        self.assertEqual(err, not ok, out)
        return out

    def test_tool_only_with_offer_right(self):
        self.assertIn("propose_offer", {t["name"] for t in tools.definitions(self.task("x", products=[]))})
        clerk = User.objects.create_user("clerk", "c@x.y", "p", is_staff=True)
        clerk.profile.role = "readonly"                                 # Angebote в ролі — лише перегляд
        clerk.profile.save()
        clerk = User.objects.get(pk=clerk.pk)                           # без кешу прав
        task = self.task("x", products=[])
        task.owner = clerk
        self.assertNotIn("propose_offer", {t["name"] for t in tools.definitions(task)})
        ctx = tools.Context(task)
        self.assertIn("немає права", self.call(ctx, ok=False))

    def test_propose_then_apply_creates_a_draft_offer(self):
        ctx = self.ctx()
        self.assertIn("2 позицій", self.call(ctx))
        p = ctx.task.proposals.get()
        self.assertEqual(p.kind, AgentProposal.OFFER)
        self.assertEqual(p.data["lines"][0], {"sku": "AN220213-01C", "description": "", "qtys": "35; 100"})
        what, old, new = decide.describe(p)
        self.assertEqual((what, old), ("📝 Angebot", ""))
        self.assertIn("Muster GmbH", new)
        self.assertEqual(Offer.objects.count(), 0)                      # до ✓ нічого не створено
        decide.apply_selected(ctx.task, [p.pk], self.user)
        p.refresh_from_db()
        self.assertEqual(p.status, AgentProposal.APPLIED, p.error)
        offer = Offer.objects.get()
        self.assertEqual((offer.customer, offer.status, offer.language), (self.customer, Offer.DRAFT, "de"))
        self.assertEqual(p.result_url, f"/offers/{offer.pk}/quick/")
        lines = list(offer.lines.order_by("pk"))
        self.assertEqual(lines[0].product, self.ant)
        self.assertEqual([t["qty"] for t in lines[0].tiers], ["35", "100"])
        self.assertEqual((lines[1].product, lines[1].description), (None, "Kabel nach Zeichnung, 150 mm"))

    def test_bad_input_is_explained(self):
        ctx = self.ctx()
        self.assertIn("немає в CRM", self.call(ctx, ok=False, customer="Unbekannt AG"))
        self.assertIn("немає на складі", self.call(ctx, ok=False, lines=[{"sku": "XX-404", "qty": "5"}]))
        self.assertIn("кількість", self.call(ctx, ok=False, lines=[{"sku": "FT220407", "qty": "0"}]))
        self.assertIn("проєкту", self.call(ctx, ok=False, project="P-2099-999"))
        self.assertEqual(ctx.task.proposals.count(), 0)
