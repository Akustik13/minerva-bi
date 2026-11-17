"""📎 Група фото однієї візитки: ще візитки тієї самої фірми, флаєр, каталог…; перезняти лицьовий / зворотний бік,
дознімати зворотний пізніше, прибрати (suppliers/services/card_photos.py, app_api card_side / card_photos_add,
сторінка фото); AI читає всі фото разом, люди з інших візиток — контактами.

python manage.py test suppliers.tests_card_photos --settings=tabele.settings_test
"""
import json
import os
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from suppliers.models import SupplierMedia, SupplierMediaPhoto
from suppliers.services import ai, card_photos, cards
from suppliers import tests_card_app
from suppliers.tests import Base, png
from suppliers.tests_card_app import jpeg

ANSWER = {
    "company": "Antenna Integrators GmbH", "website": "https://ai-gmbh.de", "email": "sales@ai-gmbh.de",
    "country": "DE", "description": "Антени для LTE.", "categories": ["Виробники плат"],
    "contact": {"name": "Max Muster", "position": "Sales", "email": "max@ai-gmbh.de"},
    "people": [{"name": "Eva Schmidt", "position": "CEO", "email": "eva@ai-gmbh.de", "mobile": "+49 170 1"},
               {"name": "Max Muster", "email": "max@ai-gmbh.de"}],
}


class CardPhotoTests(Base):
    paired = tests_card_app.CardAppTests.paired      # ключ телефону і запит до API — як у тестах додатка
    api = tests_card_app.CardAppTests.api

    def upload(self, token, uuid="u-1", **extra):
        data = {"image": SimpleUploadedFile("f.jpg", jpeg(), "image/jpeg"), "uuid": uuid, "kind": "card"}
        data.update(extra)
        r = self.api("post", "cards_app_cards", token, data=data)
        self.assertIn(r.status_code, (200, 201), r.content)
        return r.json()["card"]

    def test_add_photos_once_list_image_and_delete(self):
        token = self.paired()
        card = self.upload(token)
        pk = card["id"]
        self.assertEqual(card["photos"], [])
        add = {"image": SimpleUploadedFile("x.jpg", jpeg((0, 90, 0)), "image/jpeg"), "kind": "flyer", "uuid": "p-1",
               "note": "стенд B3"}
        r = self.api("post", "cards_app_card_photos", token, pk, data=add)
        self.assertEqual(r.status_code, 200, r.content)
        photo = r.json()["photo"]
        again = self.api("post", "cards_app_card_photos", token, pk,
                         data={"image": SimpleUploadedFile("x.jpg", jpeg(), "image/jpeg"), "kind": "flyer",
                               "uuid": "p-1"}).json()
        self.assertEqual(again["photo"]["id"], photo["id"])                       # той самий uuid — без дубля
        self.assertEqual(SupplierMediaPhoto.objects.count(), 1)
        j = self.api("get", "cards_app_card", token, pk).json()["card"]
        self.assertEqual([(p["kind"], p["note"]) for p in j["photos"]], [("flyer", "стенд B3")])
        img = self.api("get", "cards_app_card_image", token, pk, data={"side": f"p{photo['id']}"})
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img["Content-Type"], "image/jpeg")
        bad = self.api("post", "cards_app_card_photos", token, pk,
                       data={"image": SimpleUploadedFile("x.jpg", b"nope", "image/jpeg"), "uuid": "p-2"})
        self.assertEqual(bad.status_code, 400)
        path = SupplierMediaPhoto.objects.get().image.path
        r = self.api("post", "cards_app_card_photo_delete", token, pk, photo["id"])
        self.assertEqual(r.json()["card"]["photos"], [])
        self.assertFalse(os.path.exists(path))
        self.assertTrue(self.api("post", "cards_app_card_photo_delete", token, pk, photo["id"]).json()["ok"])

    def test_retake_front_add_back_later_and_drop_back(self):
        token = self.paired()
        pk = self.upload(token)["id"]
        m = SupplierMedia.objects.get(pk=pk)
        old = m.image.path
        r = self.api("post", "cards_app_card_side", token, pk,
                     data={"side": "front", "image": SimpleUploadedFile("n.jpg", jpeg((0, 0, 200)), "image/jpeg")})
        self.assertEqual(r.status_code, 200, r.content)
        m.refresh_from_db()
        self.assertNotEqual(m.image.path, old)
        self.assertFalse(os.path.exists(old))                                    # старе фото — геть
        self.assertFalse(r.json()["card"]["has_back"])
        r = self.api("post", "cards_app_card_side", token, pk,
                     data={"side": "back", "image": SimpleUploadedFile("b.jpg", jpeg(), "image/jpeg"),
                           "qr": "https://ai-gmbh.de"})
        self.assertTrue(r.json()["card"]["has_back"])                            # дознімали зворотний бік
        m.refresh_from_db()
        self.assertIn("https://ai-gmbh.de", m.qr_text)
        back = m.image_back.path
        r = self.api("post", "cards_app_card_side", token, pk, data={"side": "back", "delete": "1"})
        self.assertFalse(r.json()["card"]["has_back"])
        self.assertFalse(os.path.exists(back))
        self.assertTrue(os.path.exists(SupplierMedia.objects.get(pk=pk).image.path))
        self.assertEqual(self.api("post", "cards_app_card_side", token, pk,
                                  data={"side": "front", "delete": "1"}).status_code, 400)

    def test_someone_elses_card_needs_change_right(self):
        token = self.paired()
        pk = self.upload(token)["id"]
        m = SupplierMedia.objects.get(pk=pk)
        from suppliers.services import card_app
        with mock.patch.object(card_app, "can_edit", return_value=False):     # чуже фото без права змінювати
            r = self.api("post", "cards_app_card_photos", token, pk,
                         data={"image": SimpleUploadedFile("x.jpg", jpeg(), "image/jpeg")})
            side = self.api("post", "cards_app_card_side", token, pk, data={"side": "back", "delete": "1"})
        self.assertEqual((r.status_code, side.status_code), (403, 403))
        self.assertEqual(m.photos.count(), 0)

    def test_ai_reads_the_group_and_people_become_contacts(self):
        token = self.paired()
        pk = self.upload(token)["id"]
        m = SupplierMedia.objects.get(pk=pk)
        card_photos.add(m, SimpleUploadedFile("c2.jpg", jpeg(), "image/jpeg"), "card", self.user)
        card_photos.add(m, SimpleUploadedFile("fl.jpg", jpeg(), "image/jpeg"), "flyer", self.user, note="LTE")
        with mock.patch.object(ai, "call", return_value=(json.dumps(ANSWER), Decimal("0.04"))) as call:
            d = ai.card(m, user=self.user)
        content = call.call_args[0][1]
        labels = [b["text"] for b in content if b["type"] == "text"]
        self.assertTrue(labels[0].startswith("Фото 1 — візитка, лицьовий бік"))
        self.assertTrue(any("ще одна візитка" in x for x in labels))
        self.assertTrue(any("флаєр — LTE" in x for x in labels))
        self.assertEqual(sum(1 for b in content if b["type"] == "image"), 3)
        self.assertIn("people", call.call_args[0][0])
        self.assertEqual([p["name"] for p in d["people"]], ["Eva Schmidt"])        # людина з візитки — не вдруге
        out = cards.to_suppliers([m], self.user)
        s = out["created"][0][1]
        self.assertEqual(sorted(s.contacts.values_list("name", flat=True)), ["Eva Schmidt", "Max Muster"])
        self.assertEqual(s.contacts.get(name="Eva Schmidt").mobile, "+49 170 1")
        # дознімали ще візитку до внесеної: стан «внесено» лишається, нова людина — контакт
        m.refresh_from_db()
        card_photos.add(m, SimpleUploadedFile("c3.jpg", jpeg(), "image/jpeg"), "card", self.user)
        more = dict(ANSWER, people=[{"name": "Tom Weber", "email": "tom@ai-gmbh.de"}])
        with mock.patch.object(ai, "call", return_value=(json.dumps(more), Decimal("0.04"))):
            ai.card(m, user=self.user)
        m.refresh_from_db()
        self.assertEqual(m.state, SupplierMedia.APPLIED)
        self.assertEqual(m.cost_usd, Decimal("0.08"))
        self.assertTrue(s.contacts.filter(name="Tom Weber").exists())
        self.assertIn("Tom Weber", cards.mail_text(m))

    def test_single_card_read_keeps_web_findings(self):
        token = self.paired()
        m = SupplierMedia.objects.get(pk=self.upload(token)["id"])
        SupplierMedia.objects.filter(pk=m.pk).update(extracted={
            "addr_city": "Berlin", "from_web": ["addr_city"], "web": {"summary": "Німецький виробник."}})
        m.refresh_from_db()
        with mock.patch.object(ai, "call", return_value=(json.dumps(ANSWER), Decimal("0.02"))) as call:
            d = ai.card(m, user=self.user)
        self.assertNotIn("people", call.call_args[0][0])                         # одна візитка — без групи
        self.assertEqual((d["addr_city"], d["from_web"]), ("Berlin", ["addr_city"]))
        self.assertEqual(d["web"]["summary"], "Німецький виробник.")
        self.assertNotIn("people", d)

    def test_web_page_add_back_and_photos_delete_card_removes_files(self):
        m = cards.create(self.user, SimpleUploadedFile("c.png", png(), "image/png"))
        url = reverse("admin:inventory_supplier_card_review", args=[m.pk])
        page = self.client.get(url).content.decode()
        self.assertIn("📎 Додати фото", page)
        self.assertIn('<option value="back">↩ Зворотний бік</option>', page)
        r = self.client.post(url, {"action": "add_photo", "kind": "back", "photos": [
            SimpleUploadedFile("b.png", png(), "image/png"), SimpleUploadedFile("c2.png", png(), "image/png")]})
        self.assertEqual(r.status_code, 302)
        m.refresh_from_db()
        self.assertTrue(m.image_back)                                           # перше — зворотний бік
        self.assertEqual(list(m.photos.values_list("kind", flat=True)), ["card"])  # решта — ще фото
        p = m.photos.get()
        self.client.post(url, {"action": "photo_kind", "photo": p.pk, "kind": "catalog"})
        p.refresh_from_db()
        self.assertEqual(p.kind, "catalog")
        before = p.image.name
        self.client.post(url, {"action": "photo_rotate", "photo": p.pk})
        p.refresh_from_db()
        self.assertNotEqual(p.image.name, before)
        page = self.client.get(url).content.decode()
        self.assertIn("📎 Ще фото цієї групи (1)", page)
        self.assertNotIn('<option value="back">', page)
        tiles = self.client.get(reverse("admin:inventory_supplier_cards")).content.decode()
        self.assertIn("📎 +1", tiles)
        path = p.image.path
        cards.delete(m)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(SupplierMediaPhoto.objects.exists())
