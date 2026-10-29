"""⭐ Обране й порядок лівої колонки скриньки (nav_prefs.py):
python manage.py test email_assistant.tests_nav --settings=tabele.settings_test"""
import datetime
import json

from django.utils import timezone

from email_assistant import nav_prefs
from email_assistant.models import EmailAccount, EmailSettings, MailCategory
from email_assistant.tests_work_categories import Base


class NavPrefsTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.internal = MailCategory.objects.create(name="Internal", icon="🟦", senders="@sevskiy.example", order=2)
        self.es = EmailSettings.get_for_user(self.user)

    def nav(self, action, key):
        return self.client.post("/email/nav/", json.dumps({"action": action, "key": key}),
                                content_type="application/json")

    def order(self, es=None):
        cats = [self.dk, self.shop, self.internal]
        return [c.name for c in nav_prefs.ordered(cats, es or EmailSettings.get_for_user(self.user),
                                                  lambda c: f"wc:{c.pk}", name_of=lambda c: c.name)]

    def test_pinned_on_top_then_usage_rare_at_bottom(self):
        self.assertEqual(self.order(), ["DigiKey", "Магазин", "Internal"])         # без даних — як у налаштуваннях
        for _ in range(3):
            self.assertTrue(self.nav("use", f"wc:{self.internal.pk}").json()["ok"])
        self.nav("use", f"wc:{self.shop.pk}")
        self.assertEqual(self.order(), ["Internal", "Магазин", "DigiKey"])         # часті вгорі, рідкісні внизу
        self.assertEqual(self.nav("pin", f"wc:{self.dk.pk}").json()["pinned"], [f"wc:{self.dk.pk}"])
        self.nav("pin", "quote")
        self.assertEqual(self.order(), ["DigiKey", "Internal", "Магазин"])         # ⭐ обране — завжди зверху
        self.assertEqual(self.nav("up", "quote").json()["pinned"], ["quote", f"wc:{self.dk.pk}"])
        self.assertEqual(self.nav("unpin", "quote").json()["pinned"], [f"wc:{self.dk.pk}"])
        self.assertEqual(self.nav("pin", "evil<script>").status_code, 400)
        self.assertEqual(self.nav("drop", "quote").status_code, 400)

    def test_usage_ages_and_sort_modes(self):
        old = (timezone.localdate() - datetime.timedelta(days=60)).isoformat()
        self.es.nav_usage = {f"wc:{self.dk.pk}": [8, old]}                          # 8 кліків 2 місяці тому
        self.es.save()
        self.assertAlmostEqual(nav_prefs.scores(self.es)[f"wc:{self.dk.pk}"], 2.0, places=3)
        nav_prefs.record_use(self.es, f"wc:{self.dk.pk}")
        self.assertEqual(self.es.nav_usage[f"wc:{self.dk.pk}"], [3.0, timezone.localdate().isoformat()])
        self.es.nav_sort = "name"
        self.assertEqual(self.order(self.es), ["DigiKey", "Internal", "Магазин"])
        r = self.client.post("/email/settings/toggle/", json.dumps({"field": "nav_sort", "value": "order"}),
                             content_type="application/json")
        self.assertTrue(r.json()["ok"])
        self.assertEqual(EmailSettings.get_for_user(self.user).nav_sort, "order")

    def test_inbox_and_why_dialog_follow_the_order(self):
        msg = self.mail("orders@digikey.com", subject="Order 1")
        self.nav("pin", f"wc:{self.internal.pk}")
        self.nav("pin", "quote")
        page = self.client.get("/email/")
        html = page.content.decode()
        self.assertIn(f'data-nav="wc:{self.internal.pk}"', html)
        self.assertIn('data-nav="quote"', html)                                     # обране — навіть без запитів
        self.assertIn('id="em-nav-pins-data"', html)
        self.assertIn("showNavSort", html)
        nav = html[html.index('id="em-nav"'):]
        self.assertLess(nav.index(f'data-wcat="{self.internal.pk}"'), nav.index(f'data-wcat="{self.dk.pk}"'))
        ctx = html[html.index("Віднести до категорії"):]                            # меню листа — той самий порядок
        self.assertLess(ctx.index(f"ctxSetWorkCategory('{self.internal.pk}')"), ctx.index(f"ctxSetWorkCategory('{self.dk.pk}')"))
        why = self.client.get(f"/email/message/{msg.pk}/why/").json()
        self.assertEqual(why["categories"][0]["pk"], self.internal.pk)

    def test_message_menu_what_is_shown_and_what_goes_to_more(self):
        """Меню правого кліку листа: за замовчуванням частину сховано в «⋯ Ще», свій вибір — окремо для списку й листа."""
        self.assertEqual(nav_prefs.ctx_hidden(self.es), nav_prefs.CTX_DEFAULT_HIDDEN)

        def save(body):
            return self.client.post("/email/ctx-menu/", json.dumps(body), content_type="application/json").json()
        r = save({"list": ["star", "reply", "bogus"], "letter": ["spam", "reply"]})
        self.assertEqual(r["hidden"], {"list": ["star"], "letter": ["reply", "spam"]})   # лише відомі; reply — лише в листі
        self.assertEqual(EmailSettings.get_for_user(self.user).ctx_menu, r["hidden"])
        self.assertEqual(save({"list": []})["hidden"]["letter"], ["reply", "spam"])     # інший список не змінюється
        html = self.client.get("/email/").content.decode()
        self.assertIn('<script id="em-ctx-hidden" type="application/json">{"list": [], "letter": ["reply", "spam"]}', html)
        for key in ("sel_copy", "reply", "angebot", "read", "cat", "why", "delete"):
            self.assertIn(f'data-k="{key}"', html)
        self.assertIn('id="em-ctx-more"', html)
        self.assertIn("ctxAction('logo')", html)                                    # 🖼 значок — у меню листа
        self.assertIn("if (action === 'logo') { emLogoDialog(_ctxMsg.from", html)
        self.assertEqual(save({"reset": True})["hidden"], nav_prefs.CTX_DEFAULT_HIDDEN)
        bad = self.client.post("/email/ctx-menu/", "[1]", content_type="application/json")
        self.assertEqual(bad.status_code, 400)

    def test_left_column_bottom_tabs(self):
        """Ліва колонка — вкладки внизу (як в Outlook): папки, категорії, проєкти, клієнти, «⋯ Ще»."""
        html = self.client.get("/email/").content.decode()
        self.assertIn('id="em-p1-tabs"', html)
        for pane in ("mail", "cats", "proj", "cust", "more"):
            self.assertIn(f'class="em-p1-tab" data-pane="{pane}"', html)
            self.assertIn(f'data-pane="{pane}">', html)
        more = html[html.index('data-pane="more">'):]
        self.assertLess(more.index("openSpamFilter()"), more.index('id="em-p1-tabs"'))   # «Фільтри спаму» — у «⋯ Ще»
        self.assertNotIn('id="em-sync-more"', html)
        self.assertIn("Відкритих проєктів з листами цієї скриньки немає", html)
