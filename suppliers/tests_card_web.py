"""🌐 Візитка → пошук фірми в інтернеті; 🗂 значок категорії — палітра, свій емодзі, своя картинка.

python manage.py test suppliers.tests_card_web --settings=tabele.settings_test
"""
import io
import json
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from suppliers.models import SupplierCategory, SupplierMedia
from suppliers.services import ai, cards
from suppliers.tests import Base, png


ANSWER = {
    "company": "Antenna Integrators GmbH", "website": "https://ai-gmbh.de", "email": "sales@ai-gmbh.de",
    "phone": "+49 30 123", "street": "Hauptstr. 1", "zip": "10115", "city": "Berlin", "country": "DE",
    "vat_id": "DE123456789", "description": "Розробляють і виготовляють антени для LTE.",
    "categories": ["Виробники плат"], "products": ["LTE antennas"],
    "contact": {"name": "Eva Schmidt", "position": "Sales", "email": "eva@ai-gmbh.de"},
    "people": [{"name": "Tom Weber", "position": "CEO", "email": "tom@ai-gmbh.de"}],
    "summary": "Німецький виробник антен.", "role": "customer", "role_why": "Інтегрують антени",
    "sources": [{"title": "Impressum", "url": "https://ai-gmbh.de/impressum"}, {"title": "x", "url": "javascript:1"}]}


class CardWebTests(Base):
    def card(self, extracted):
        m = cards.create(self.user, SimpleUploadedFile("c.png", png(), "image/png"))
        SupplierMedia.objects.filter(pk=m.pk).update(extracted=extracted, state=SupplierMedia.READ)
        m.refresh_from_db()
        return m

    def test_fills_only_empty_fields_and_keeps_the_card_person(self):
        m = self.card({"website": "ai-gmbh.de", "contact_name": "Max Muster", "description": "Схоже, антени.",
                       "phone": "+49 30 999"})
        with mock.patch.object(ai, "call", return_value=(json.dumps(ANSWER), Decimal("0.05"))) as call:
            d = ai.web_card(m, user=self.user)
        self.assertTrue(call.call_args.kwargs["web"])                               # з веб-пошуком
        self.assertIn("Сайт: ai-gmbh.de", call.call_args[0][1])
        self.assertEqual(d["name"], "Antenna Integrators GmbH")
        self.assertEqual(d["phone"], "+49 30 999")                                 # з візитки — не чіпаємо
        self.assertEqual(d["description"].split("\n")[0], "Розробляють і виготовляють антени для LTE.")  # «Схоже» — так
        self.assertEqual(d["contact_name"], "Max Muster")                          # інша людина — не в контакт
        self.assertNotIn("contact_email", d.get("from_web"))
        self.assertEqual([p["name"] for p in d["web"]["people"]], ["Eva Schmidt", "Tom Weber"])
        self.assertIn("addr_city", d["from_web"])
        self.assertEqual([s["url"] for s in d["web"]["sources"]], ["https://ai-gmbh.de/impressum"])
        m.refresh_from_db()
        self.assertEqual((m.role, m.role_ai), ("customer", True))
        self.assertEqual([c.name for c in m.categories.all()], ["Виробники плат"])
        self.assertEqual(m.cost_usd, Decimal("0.05"))

    def test_page_button_job_and_marks(self):
        m = self.card({"website": "ai-gmbh.de"})
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        self.assertIn("🌐 Знайти в інтернеті", self.client.get(url).content.decode())
        with mock.patch.object(ai, "call", return_value=(json.dumps(ANSWER), Decimal("0.05"))):
            job = self.client.post(url, {"action": "web"}).json()["job"]
        from suppliers.services import jobs
        self.assertIn("Дописано полів", jobs.state(job, self.user)["result"]["message"])
        page = self.client.get(url).content.decode()
        self.assertIn("🌐 Знайдено в інтернеті", page)
        self.assertIn("Німецький виробник антен.", page)
        self.assertIn("Tom Weber", page)
        self.assertIn('value="Antenna Integrators GmbH"', page)
        self.assertIn('"addr_city"', page)                                         # позначка 🌐 у полях

    def test_unread_card_sends_the_photo(self):
        m = self.card({})
        with mock.patch.object(ai, "call", return_value=(json.dumps(ANSWER), Decimal("0.05"))) as call:
            ai.web_card(m, user=self.user)
        content = call.call_args[0][1]
        self.assertEqual(content[0]["type"], "image")


class CategoryIconTests(Base):
    def test_palette_upload_and_icon_everywhere(self):
        cat = SupplierCategory.objects.create(name="Клієнти", icon="")
        url = reverse("admin:suppliers_suppliercategory_change", args=[cat.pk])
        page = self.client.get(url).content.decode()
        self.assertIn('class="ip-pal"', page)
        self.assertIn('data-v="🏭"', page)
        self.assertIn('enctype="multipart/form-data"', page)
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (600, 300), (255, 0, 0, 255)).save(buf, "PNG")
        data = {"name": "Клієнти", "icon": "👥", "color": "#5b8def", "order": 100, "description": "",
                "request_template": "", "is_active": "on",
                "icon_image": SimpleUploadedFile("logo.png", buf.getvalue(), "image/png")}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        cat.refresh_from_db()
        self.assertEqual(cat.icon, "👥")
        self.assertTrue(cat.icon_image.name.startswith("supplier_categories/"))
        with cat.icon_image.open("rb") as fh:
            img = Image.open(fh)
            self.assertLessEqual(max(img.size), 128)
        self.assertIn("<img", str(cat.icon_html))
        changelist = self.client.get(reverse("admin:suppliers_suppliercategory_changelist")).content.decode()
        self.assertIn('class="sp-cat-img"', changelist)
        # не картинка — відмова, збережене лишається
        bad = dict(data, icon_image=SimpleUploadedFile("x.png", b"not an image", "image/png"))
        res = self.client.post(url, bad)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "PNG, JPG, GIF або WEBP")
        # без картинки — емодзі
        self.assertEqual(str(SupplierCategory(name="x", icon="🔌").icon_html), "🔌")
