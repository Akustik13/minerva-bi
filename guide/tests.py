"""📚 Гід по Minerva: рендер, завантаження, перевірка посилань, API з коду, сторінки — і що справжній гід цілий.

python manage.py test guide --settings=tabele.settings_test
"""
import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from guide import api_docs, check, loader
from guide import render as md

PAGE = """---
title: Тестова сторінка
order: 2
summary: Для тесту.
where: Система → Тест
urls: /admin/auth/user/
keywords: тест, приклад, Testseite
related: demo/other
---
## Що це

Текст з **жирним**, *курсивом* і `кодом`, [гід](guide:demo/other#kroky), [користувачі](/admin/auth/user/).
"""
OTHER = """---
title: Інша сторінка
summary: Друга.
where: Система → Інше
---
## Кроки
1. Перший
2. Другий
"""


class RenderTests(TestCase):
    def test_escaping_and_links(self):
        html, _ = md.render('<script>alert(1)</script> [x](javascript:alert(1)) [y](https://example.com) '
                            '[z](guide:mail/inbox#filters) [w](/email/)')
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("javascript:", html)                                      # небезпечне — лише текст
        self.assertIn('href="https://example.com" class="gd-ext" target="_blank" rel="noopener noreferrer"', html)
        self.assertIn('href="/guide/mail/inbox/#filters"', html)
        self.assertIn('href="/email/"', html)

    def test_blocks(self):
        text = ("# Заголовок\n\n## Кроки налаштування\n\n1. Перший\n   продовження\n2. Другий\n  - вкладений\n\n"
                "| A | B |\n|---|---|\n| 1 | **2** |\n\n> 💡 Порада\n>\n> другий абзац\n\n"
                "```bash\ncurl {{base_url}}/api/v1/ <x>\n```\n\n{{api_endpoints:stock}}\n")
        html, toc = md.render(text, "https://m.example", directives=lambda name, arg: f"<i>{name}:{arg}</i>")
        self.assertEqual(toc, [(2, "кроки-налаштування", "Кроки налаштування")])
        self.assertIn("<ol><li>Перший продовження</li><li>Другий<ul><li>вкладений</li></ul></li></ol>", html)
        self.assertIn("<td><strong>2</strong></td>", html)
        self.assertIn('class="gd-callout gd-tip"', html)
        start = html.index("gd-callout")
        self.assertEqual(html[start:html.index("</div>", start)].count("<p>"), 2)          # «>» порожній — новий абзац
        self.assertIn("curl https://m.example/api/v1/ &lt;x&gt;", html)               # код екранується
        self.assertIn("<i>api_endpoints:stock</i>", html)

    def test_links_ignore_code(self):
        self.assertEqual(md.links("[a](/x/) `[b](/y/)`\n```\n[c](/z/)\n```\n"), ["/x/"])


class LoaderAndCheckTests(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        guide = os.path.join(self.root, "docs", "guide")
        os.makedirs(os.path.join(guide, "demo"))
        self.write("demo/_section.md", "---\ntitle: Демо\nicon: 🧪\norder: 1\n---\nВступ.\n")
        self.write("demo/page.md", PAGE)
        self.write("demo/other.md", OTHER)
        self.guide = guide
        o = override_settings(GUIDE_DIR=guide)
        o.enable()
        self.addCleanup(o.disable)

    def write(self, rel, text):
        path = os.path.join(self.root, "docs", "guide", rel)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.utime(path)

    def test_load_parse_search(self):
        sections = loader.load()
        self.assertEqual([(s.key, s.title, s.icon) for s in sections], [("demo", "Демо", "🧪")])
        page = loader.page("demo/page")
        self.assertEqual((page.title, page.urls, page.keywords[-1], page.related), (
            "Тестова сторінка", ["/admin/auth/user/"], "Testseite", ["demo/other"]))
        self.assertEqual([p.slug for p in sections[0].pages], ["demo/page", "demo/other"])   # order 2 < 100
        self.assertEqual(loader.search("testseite")[0][0].slug, "demo/page")
        self.assertEqual(loader.search("кроки")[0][0].slug, "demo/other")
        self.assertEqual(loader.search(""), [])
        self.assertEqual(check.check(), [])

    def test_check_finds_broken_links(self):
        self.write("demo/bad.md", "---\ntitle: Погана\nsummary: s\nwhere: w\nurls: /no/such/page/\n"
                                  "related: demo/missing\n---\n[a](/nope/) [b](guide:demo/gone) [c](ftp://x) "
                                  "[d](/admin/no/such/) [e](/admin/auth/user/nonsense/)\n"
                                  "{{api_endpoints:no-such-prefix}}\n")
        self.write("demo/empty.md", "---\ntitle: Порожня\n---\n")
        errors = "\n".join(check.check())
        for needle in ("адреси Minerva немає «/nope/»", "немає сторінки гіда «demo/gone»", "незрозуміле посилання",
                       "адреси Minerva немає «/admin/no/such/»",                  # адмінка ловить будь-яку адресу
                       "адреси Minerva немає «/admin/auth/user/nonsense/»",
                       "urls — адреси Minerva немає «/no/such/page/»", "related — немає сторінки «demo/missing»",
                       "таких адрес API немає", "docs/guide/demo/empty.md: у шапці немає «summary»"):
            self.assertIn(needle, errors)

    def test_views(self):
        r = self.client.get("/guide/")
        self.assertEqual(r.status_code, 302)                                          # лише після входу
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True                                              # інакше — майстер першого входу
        s.save()
        user = User.objects.create_user("staff", "s@x.y", "p", is_staff=True)
        user.profile.role = "admin"
        user.profile.save()
        self.client.force_login(user)
        r = self.client.get("/guide/")
        self.assertContains(r, "Тестова сторінка")
        self.assertContains(r, "🧪 Демо")
        r = self.client.get("/guide/demo/page/")
        self.assertContains(r, "<strong>жирним</strong>", html=False)
        self.assertContains(r, 'href="/guide/demo/other/#kroky"')
        self.assertContains(r, "Відкрити →")
        self.assertContains(r, "Інша сторінка")                                       # пов'язане й наступна
        self.assertEqual(self.client.get("/guide/demo/nope/").status_code, 404)
        data = self.client.get("/guide/api/search/", {"q": "приклад"}).json()
        self.assertEqual(data["results"][0]["url"], "/guide/demo/page/")
        self.assertContains(self.client.get("/guide/", {"q": "кроки"}), "Знайдено за «кроки»")


class ApiDocsTests(TestCase):
    def test_endpoints_from_code(self):
        eps = {e["path"]: e for e in api_docs.endpoints()}
        stock = {m["method"]: m for m in eps["/api/v1/stock/"]["methods"]}
        self.assertEqual(stock["GET"]["scope"], "stock:read")
        check_ = eps["/api/v1/stock/check/"]["methods"][0]
        self.assertEqual((check_["method"], check_["scope"]), ("POST", "stock:read"))   # action_scopes
        orders = {m["method"]: m for m in eps["/api/v1/orders/"]["methods"]}
        self.assertEqual(orders["POST"]["scope"], "orders:write")
        self.assertIn("order_number", orders["POST"]["body"])
        self.assertTrue(all(not e["path"].endswith("{format}") for e in eps.values()))
        self.assertTrue(api_docs.endpoints("purchase-orders"))
        curl = api_docs.curl("https://m.example", "/api/v1/orders/", "POST", {"a": 1})
        self.assertIn('curl -X POST "https://m.example/api/v1/orders/"', curl)
        self.assertIn("-d '{\"a\": 1}'", curl)
        html = api_docs.render_html("https://m.example", "stock")
        self.assertIn("/api/v1/stock/check/", html)
        self.assertIn("🔑 stock:read", html)


class RealGuideTests(TestCase):
    def test_every_page_and_link_is_valid(self):
        """Справжній гід docs/guide/: шапки, усі посилання й адреси живі (те саме, що manage.py guide_check)."""
        self.assertTrue(loader.pages(), "гід порожній")
        self.assertEqual(check.check(), [])
