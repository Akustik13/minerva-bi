"""Підказки в пошуку сайту (shop/search_assist.py, GET /api/v1/shop/search/assist/): журнал запитів, «часто купують
разом», AI-підказки (модель підмінено, без мережі). python manage.py test shop --settings=tabele.settings_test"""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import APIKey
from inventory.models import Product
from sales.models import SalesOrder, SalesOrderLine

from . import search_assist
from .models import Shop, ShopListing, ShopSearchQuery

URL = "/api/v1/shop/search/assist/"


def fake_response(tips, usage=(5000, 60, 4800, 0), stop="end_turn"):
    text = json.dumps({"tips": tips})
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, model="claude-opus-5-5",
                           usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1],
                                                 cache_read_input_tokens=usage[2], cache_creation_input_tokens=usage[3]))


class SearchAssistTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.shop = Shop.objects.create(name="Web", slug="webshop", search_assist=Shop.SEARCH_RULES)
        self.p = {}
        for sku, name in [("AN-868", "Antenne 868 MHz MHF1"), ("CA-MHF1", "Kabel MHF1 auf SMA"), ("AN-GNSS", "GNSS-Antenne"),
                          ("HIDDEN", "Versteckt")]:
            self.p[sku] = Product.objects.create(sku=sku, name=name, sale_price=Decimal("5"),
                                                 tech_attributes={"Frequency Range": "863-870 MHz", "hts": "8529"})
            ShopListing.objects.create(shop=self.shop, product=self.p[sku], is_visible=sku != "HIDDEN")
        key = APIKey.objects.create(name="site", shop=self.shop, scopes=["products:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")

    def order(self, number, *skus):
        o = SalesOrder.objects.create(source="manual", order_number=number, client="X")
        for sku in skus:
            SalesOrderLine.objects.create(order=o, product=self.p[sku], qty=1)

    def test_config_log_and_together(self):
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_OFF)
        self.assertEqual(self.c.get(URL, {"config": "1"}).json(), {"mode": "off"})
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_RULES)
        self.order("A", "AN-868", "CA-MHF1")
        self.order("B", "AN-868", "CA-MHF1", "HIDDEN")
        self.order("C", "AN-868", "AN-GNSS")
        d = self.c.get(URL, {"q": "  Antenne  868 ", "n": "0", "skus": "AN-868"}).json()
        self.assertEqual(d["mode"], "rules")
        self.assertEqual(d["together"], [{"sku": "CA-MHF1", "orders": 2}])   # прихований і разовий — ні
        self.assertNotIn("ai", d)
        self.c.get(URL, {"q": "antenne 868", "n": "3", "skus": ""})
        row = ShopSearchQuery.objects.get(shop=self.shop, query="antenne 868")
        self.assertEqual((row.searches, row.zero_results, row.last_results), (2, 1, 3))
        other = APIClient()
        other.credentials(HTTP_AUTHORIZATION="Token " + APIKey.objects.create(name="rag", scopes=["products:read"]).key)
        self.assertEqual(other.get(URL, {"config": "1"}).status_code, 403)   # лише ключ, прив'язаний до магазину

    @mock.patch("shop.search_assist._call")
    def test_ai_tips_validated_cached_limited(self, call):
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_AI, search_ai_daily_limit=2)
        call.return_value = fake_response([{"sku": "CA-MHF1", "reason": "Passendes Kabel für MHF1."},
                                           {"sku": "AN-868", "reason": "schon in den Treffern"},
                                           {"sku": "NOPE-1", "reason": "erfunden"},
                                           {"sku": "HIDDEN", "reason": "versteckt"}])
        d = self.c.get(URL, {"q": "868 mhz", "n": "1", "skus": "AN-868", "ai": "1", "lang": "de"}).json()
        self.assertEqual((d["ai"]["status"], d["ai"]["cached"]), ("ok", False))
        self.assertEqual(d["ai"]["tips"], [{"sku": "CA-MHF1", "reason": "Passendes Kabel für MHF1."}])
        system = call.call_args[0][1]
        self.assertIn("CA-MHF1 | Kabel MHF1 auf SMA", system)
        self.assertNotIn("hts", system)            # службові коди DigiKey — ні
        self.assertNotIn("HIDDEN", system)
        again = self.c.get(URL, {"q": "868 MHz", "n": "1", "skus": "AN-868", "ai": "1", "lang": "de", "log": "0"}).json()
        self.assertTrue(again["ai"]["cached"])
        self.assertEqual(call.call_count, 1)       # з кешу — без токенів
        self.shop.refresh_from_db()
        u = self.shop.search_ai_usage
        self.assertEqual((u["calls"], u["cached"], u["input"], u["cache_read"], u["output"]), (1, 1, 5000, 4800, 60))
        # Opus 5.5: 5000 × $4 + 4800 × $0,20 + 60 × $20 за 1 млн токенів = $0,02216
        self.assertEqual((u["cost_musd"], u["total"]["cost_musd"]), (22160, 22160))
        self.assertEqual(search_assist.spent(u), "$0.0222")
        self.c.get(URL, {"q": "gnss", "n": "1", "skus": "AN-GNSS", "ai": "1"})
        limited = self.c.get(URL, {"q": "kabel sma", "n": "1", "skus": "", "ai": "1"}).json()
        self.assertEqual(limited["ai"]["status"], "limit")   # денний ліміт — лише правила
        self.assertEqual(ShopSearchQuery.objects.get(query="868 mhz").searches, 1)   # log=0 — не рахується

    @mock.patch("shop.search_assist._call")
    def test_ai_problem_shown_until_success(self, call):
        """Anthropic відмовив (немає кредитів) → зрозуміла причина в картці; успішний виклик її знімає."""
        class ApiError(Exception):
            def __init__(self, status, kind, message):
                super().__init__(message)
                self.status_code, self.message = status, message
                self.body = {"type": "error", "error": {"type": kind, "message": message}}

        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_AI)
        self.shop.refresh_from_db()
        key = mock.patch("strategy.models.AISettings.get", return_value=SimpleNamespace(anthropic_api_key="test"))
        key.start()
        self.addCleanup(key.stop)
        call.side_effect = ApiError(400, "invalid_request_error", "Your credit balance is too low to access the API.")
        self.assertEqual(self.c.get(URL, {"q": "868 mhz", "n": "0", "ai": "1"}).json()["ai"]["status"], "unavailable")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.search_ai_usage["ai_problem"]["code"], "billing")
        self.assertIn("закінчились кредити", search_assist.ai_ready(self.shop))
        from .onboarding import steps
        step = [x for x in steps(self.shop) if x["key"] == "search"][0]
        self.assertEqual(step["state"], "warn")
        self.assertIn("Credits kaufen", step["hint"])
        self.assertEqual(search_assist.problem_of(ApiError(402, "billing_error", "x"))["code"], "billing")
        self.assertEqual(search_assist.problem_of(ApiError(401, "authentication_error", "x"))["code"], "auth")
        self.assertEqual(search_assist.problem_of(ApiError(404, "not_found_error", "x"))["code"], "model")
        self.assertEqual(search_assist.problem_of(TimeoutError("timed out"))["code"], "unavailable")
        cache.delete(f"shop-search-ai-down:{self.shop.pk}")       # минуло 5 хв, кредити поповнили
        call.side_effect, call.return_value = None, fake_response([{"sku": "CA-MHF1", "reason": "Kabel."}])
        self.assertEqual(self.c.get(URL, {"q": "868 mhz", "n": "0", "ai": "1"}).json()["ai"]["status"], "ok")
        self.shop.refresh_from_db()
        self.assertNotIn("ai_problem", self.shop.search_ai_usage)
        self.assertEqual(search_assist.ai_ready(self.shop), "")

    def test_cost_backfill_and_models(self):
        """Лічильники без вартості (до оновлення) — оцінка за поточною моделлю; ціна — моделі, що відповіла."""
        self.assertAlmostEqual(search_assist.cost_usd("claude-sonnet-5-5", input=1_000_000, output=100_000), 3.0)
        self.assertAlmostEqual(search_assist.cost_usd("claude-haiku-5-5", cache_write=1_000_000), 0.125)
        self.assertEqual(search_assist.cost_usd("unknown-model", input=10), 0.0)
        Shop.objects.filter(pk=self.shop.pk).update(search_ai_model="claude-sonnet-5-5", search_ai_usage={
            "total": {"calls": 3, "input": 1_000_000, "output": 0}})
        self.shop.refresh_from_db()
        search_assist._count(self.shop, calls=1, input=1000, cost_musd=4000)
        total = self.shop.search_ai_usage["total"]
        self.assertEqual((total["calls"], total["cost_musd"]), (4, 2_004_000))   # $2 оцінка + $0,004
        self.assertEqual(search_assist.spent(total), "$2.00")

    @mock.patch("shop.search_assist._call", side_effect=RuntimeError("no_api_key"))
    def test_ai_failure_falls_back(self, call):
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_AI)
        d = self.c.get(URL, {"q": "868", "n": "0", "ai": "1"}).json()
        self.assertEqual(d["ai"], {"status": "unavailable", "tips": []})
        self.c.get(URL, {"q": "gnss", "n": "0", "ai": "1"})
        self.assertEqual(call.call_count, 1)       # 5 хв без звернень після збою
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_RULES)
        self.assertEqual(self.c.get(URL, {"q": "x y z", "n": "0", "ai": "1"}).json()["ai"]["status"], "off")

    def test_card_and_report(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        search_assist.record(self.shop, "rp-sma 2,4 ghz", 0)
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        page = self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/")
        self.assertContains(page, "🔎 Пошук на сайті")
        self.assertContains(page, "«rp-sma 2,4 ghz» ×1")
        self.assertContains(page, "RAG сьогодні")
        self.assertContains(page, "витрачено ≈ $0.0000")
        self.assertContains(page, "<li><b>AI-підказки RAG</b>")      # пояснення режимів — HTML, не екрановано
        report = self.client.get("/admin/shop/shopsearchquery/?zero=1")
        self.assertContains(report, "rp-sma 2,4 ghz")


class RagTipsTests(TestCase):
    """Підказки нашого RAG: завдання → опитування → товари, перевірені за асортиментом (RAG підмінено)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        from rag_assistant.models import RagSettings
        RagSettings.objects.update_or_create(pk=1, defaults={"base_url": "https://rag.example", "api_key": "k"})
        self.shop = Shop.objects.create(name="Web", slug="webshop", search_assist=Shop.SEARCH_RAG)
        for sku in ("AN-BLE", "CA-MHF1", "HIDDEN"):
            p = Product.objects.create(sku=sku, name=sku, sale_price=Decimal("5"))
            ShopListing.objects.create(shop=self.shop, product=p, is_visible=sku != "HIDDEN")
        key = APIKey.objects.create(name="site", shop=self.shop, scopes=["products:read"])
        self.c = APIClient()
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {key.key}")
        patcher = mock.patch("shop.search_assist._rag_client")
        self.rag = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.rag.ask.return_value = "job-1"

    def get(self, q, **extra):
        return self.c.get(URL, {"q": q, "n": "2", "skus": "", "rag": "1", "lang": "de", **extra}).json()

    def test_job_poll_validate_cache(self):
        self.rag.job.return_value = {"status": "running", "stage": "search"}
        d = self.get("ble and gnss")
        self.assertEqual(d["rag"]["status"], "pending")
        question = self.rag.ask.call_args[0][0]
        self.assertIn("«ble and gnss»", question)
        self.assertEqual(self.rag.ask.call_args[1]["language"], "de")
        self.assertEqual(self.get("ble and gnss", poll="1")["rag"]["status"], "pending")
        self.assertNotIn("together", self.get("ble and gnss", poll="1"))      # опитування — лише RAG
        answer = ("Für BLE und GNSS eignet sich eine Kombiantenne mit zwei Ports [1].\n\n"
                  "1. **AN-BLE** — Dualband-Antenne für Bluetooth und GNSS L1 [2].\n"
                  "| AN-BLE | 2,4 GHz |\n- **HIDDEN** — versteckt\n- NOPE-123 — erfunden\n"
                  "Dazu passt das Kabel CA-MHF1 mit MHF1-Stecker.")
        self.rag.job.return_value = {"status": "completed", "result": {
            "answer": answer, "provider": "anthropic", "model": "claude-opus-5",
            "products": [{"product": "AN-BLE", "source": "a.pdf", "page": 2}, {"product": "NOPE-123"}, {"sku": "HIDDEN"}]}}
        d = self.get("ble and gnss", poll="1")["rag"]
        self.assertEqual((d["status"], d["cached"]), ("ok", False))
        self.assertEqual(d["tips"], [{"sku": "AN-BLE", "reason": "Dualband-Antenne für Bluetooth und GNSS L1."},
                                     {"sku": "CA-MHF1", "reason": "Dazu passt das Kabel mit MHF1-Stecker."}])
        self.assertEqual(d["note"], "Für BLE und GNSS eignet sich eine Kombiantenne mit zwei Ports.")
        # кеш спільний для магазинів (RAG знає всі товари), перевірка — за асортиментом кожного магазину
        again = self.get("BLE  and gnss")["rag"]
        self.assertTrue(again["cached"])
        self.assertEqual(self.rag.ask.call_count, 1)
        self.shop.refresh_from_db()
        u = self.shop.search_ai_usage
        self.assertEqual((u["rag_calls"], u["rag_cached"]), (1, 1))
        self.assertEqual((u["rag_model"]["provider"], u["rag_model"]["model"]), ("anthropic", "claude-opus-5"))
        self.assertFalse(search_assist.rag_is_local(self.shop))
        self.assertIn("оплачується ключем anthropic на RAG-ПК", search_assist.rag_model_text(self.shop))
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.assertContains(self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/"), "RAG відповідає: anthropic / claude-opus-5")
        search_assist._count(self.shop, rag_model={"provider": "ollama", "model": "qwen2.5:7b"})
        self.assertTrue(search_assist.rag_is_local(self.shop))
        self.assertIn("без оплати", search_assist.rag_model_text(self.shop))

    def test_busy_parallel_failure_and_modes(self):
        from rag_assistant.client import RagError
        self.rag.job.return_value = {"status": "running"}
        self.assertEqual(self.get("gnss antenne")["rag"]["status"], "pending")
        self.assertEqual(self.get("lora 868")["rag"]["status"], "busy")       # одне завдання до RAG одночасно
        self.assertEqual(self.rag.ask.call_count, 1)
        cache.clear()
        self.rag.ask.side_effect = RagError("busy", "busy", 429)
        self.assertEqual(self.get("lora 868")["rag"]["status"], "busy")
        self.rag.ask.side_effect = RagError("network", "down")
        self.assertEqual(self.get("wifi 6")["rag"]["status"], "unavailable")
        self.assertEqual(self.get("uwb antenne")["rag"]["status"], "unavailable")
        self.assertEqual(self.rag.ask.call_count, 3)                          # 5 хв без звернень після збою
        self.assertIn("5 хв", search_assist.rag_ready(self.shop))
        Shop.objects.filter(pk=self.shop.pk).update(search_assist=Shop.SEARCH_AI)
        self.assertEqual(self.get("wifi 6")["rag"]["status"], "off")

    def test_lost_job_and_unconfigured(self):
        self.rag.job.return_value = {"status": "running"}
        self.get("gnss antenne")
        import hashlib
        jobkey = "shop-search-rag-job:" + hashlib.sha256("de|gnss antenne".encode()).hexdigest()[:32]
        cache.set(jobkey, {**cache.get(jobkey), "at": 1})                    # завдання «зависло» в RAG
        self.assertEqual(self.get("gnss antenne", poll="1")["rag"]["status"], "unavailable")
        self.assertEqual(self.get("gnss antenne")["rag"], {"status": "ok", "tips": [], "note": "", "cached": True})
        cache.clear()
        from rag_assistant.models import RagSettings
        RagSettings.objects.filter(pk=1).update(api_key="")
        self.assertEqual(self.get("wifi 6")["rag"]["status"], "unavailable")
        self.assertIn("ключ RAG API", search_assist.rag_ready(self.shop))
        from .onboarding import steps
        step = [x for x in steps(self.shop) if x["key"] == "search"][0]
        self.assertEqual(step["state"], "warn")
        self.assertIn("RAG-підказки", step["hint"])

    def test_rag_now_button(self):
        """«Запитати RAG-ПК, яка модель зараз»: /v1/health → llm; старий RAG без llm — підказка перезапустити API."""
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        url = f"/admin/shop/shop/{self.shop.pk}/rag-model/"
        self.assertContains(self.client.get(f"/admin/shop/shop/{self.shop.pk}/change/"), "Запитати RAG-ПК, яка модель зараз")
        self.assertEqual(self.client.get(url).status_code, 403)                  # лише POST
        self.rag.health.return_value = {"status": "ok", "api_version": "1"}
        d = self.client.post(url).json()
        self.assertFalse(d["ok"])
        self.assertIn("RAG-Integration-API", d["text"])
        self.rag.health.return_value = {"status": "ok", "llm": {"provider": "anthropic", "model": "claude-opus-5",
                                                                "analyzer_provider": "ollama", "analyzer_model": "qwen2.5:7b"}}
        d = self.client.post(url).json()
        self.assertEqual((d["ok"], d["local"]), (True, False))
        self.assertIn("anthropic / claude-opus-5 · аналізатор запиту: ollama / qwen2.5:7b", d["text"])
        self.assertIn("запит до RAG-ПК", d["text"])
        self.rag.health.return_value = {"status": "ok", "llm": {"provider": "ollama", "model": "qwen2.5:7b"}}
        self.assertTrue(self.client.post(url).json()["local"])
        from rag_assistant.client import RagError
        self.rag.health.side_effect = RagError("network", "Немає зв'язку з помічником.")
        self.assertIn("не відповів", self.client.post(url).json()["text"])

    def test_shared_cache_in_production_settings(self):
        """Обидва процеси gunicorn мають бачити ті самі завдання RAG і кеш AI — не кеш у пам'яті процесу."""
        import importlib
        prod = importlib.import_module("tabele.settings")
        self.assertTrue(prod.CACHES["shared"]["BACKEND"].endswith("FileBasedCache"))
        from core.shared_cache import cache as shared
        self.assertIs(search_assist.cache, shared)

    def test_abandoned_job_frees_queue(self):
        self.rag.job.return_value = {"status": "running"}
        self.get("gnss antenne")                       # відвідувач пішов далі, це завдання ніхто не опитує
        self.rag.job.return_value = {"status": "completed", "result": {"answer": "x", "products": [{"product": "AN-BLE"}]}}
        self.assertEqual(self.get("lora 868")["rag"]["status"], "pending")   # Minerva дочитала його сама
        self.assertEqual(self.rag.ask.call_count, 2)
        self.assertEqual([t["sku"] for t in self.get("gnss antenne")["rag"]["tips"]], ["AN-BLE"])
