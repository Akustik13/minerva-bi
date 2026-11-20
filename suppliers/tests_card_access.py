"""👁 Візитки 👥 «бачать усі» / 🔒 «лише я», хто зняв (з роллю) і звідки (📱 телефон / 🌐 сторінка / 🏭 картка
постачальника), «чий це QR» до підключення телефона, публічна сторінка додатка (suppliers/services/card_access.py).

python manage.py test suppliers.tests_card_access --settings=tabele.settings_test
"""
import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from suppliers import tests_card_app
from suppliers.models import SupplierMedia
from suppliers.services import card_access, card_app, cards
from suppliers.tests import Base, png
from suppliers.tests_card_app import jpeg


class CardAccessTests(Base):
    paired = tests_card_app.CardAppTests.paired
    api = tests_card_app.CardAppTests.api

    def setUp(self):
        super().setUp()
        self.boss = User.objects.create_superuser("boss", "boss@sevskiy.de", "p")      # навіть адміністратор
        self.mine = cards.create(self.user, SimpleUploadedFile("a.png", png(), "image/png"), title="Моя приватна",
                                 visibility="private", source=SupplierMedia.S_WEB)
        self.team = cards.create(self.boss, SimpleUploadedFile("b.png", png(), "image/png"), title="Спільна")

    def test_private_card_only_for_its_author(self):
        lst = reverse("admin:inventory_supplier_cards")
        page = self.client.get(lst).content.decode()
        self.assertIn("Моя приватна", page)
        self.assertIn('class="spc-lock"', page)
        self.client.force_login(self.boss)
        page = self.client.get(lst).content.decode()
        self.assertNotIn("Моя приватна", page)
        self.assertIn("Спільна", page)
        url = reverse("admin:inventory_supplier_card_review", args=[self.mine.pk])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "visibility", "visibility": "team"}).status_code, 404)
        # автор своєї: 🔒 і назад 👥
        team_url = reverse("admin:inventory_supplier_card_review", args=[self.team.pk])
        self.assertEqual(self.client.post(team_url, {"action": "visibility", "visibility": "private"}).status_code, 302)
        self.team.refresh_from_db()
        self.assertEqual(self.team.visibility, "private")
        self.client.post(team_url, {"action": "visibility", "visibility": "team"})
        # чужу спільну зробити приватною не можна — лише автор
        self.client.force_login(self.user)
        r = self.client.post(reverse("admin:inventory_supplier_card_review", args=[self.team.pk]),
                             {"action": "visibility", "visibility": "private"})
        self.assertEqual(r.status_code, 404)
        self.team.refresh_from_db()
        self.assertEqual(self.team.visibility, "team")
        # автор робить свою спільною
        self.client.post(url, {"action": "visibility", "visibility": "team"})
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.visibility, "team")

    def test_who_and_source_on_card_page_and_tile(self):
        self.user.profile.role = "admin"
        self.user.profile.save()
        url = reverse("admin:inventory_supplier_card_review", args=[self.mine.pk])
        page = self.client.get(url).content.decode()
        self.assertIn("🔑 Адміністратор", page)
        self.assertIn("🌐 Сторінка «📷 Візитка / флаєр»", page)
        self.assertIn('name="visibility"', page)                         # автор може змінити
        tile = self.client.get(reverse("admin:inventory_supplier_cards")).content.decode()
        self.assertIn("spc-f-who", tile)
        # з картки постачальника
        r = self.client.post(reverse("admin:inventory_supplier_card") + f"?supplier={self.supplier.pk}",
                             {"image": SimpleUploadedFile("c.png", png(), "image/png"), "kind": "card",
                              "supplier": self.supplier.pk, "visibility": "private"})
        self.assertEqual(r.status_code, 302)
        m = SupplierMedia.objects.order_by("-pk").first()
        self.assertEqual((m.source, m.visibility), (SupplierMedia.S_SUPPLIER, "private"))
        # у картці постачальника чужа приватна — не видно
        self.client.force_login(self.boss)
        self.assertFalse(card_access.visible(self.supplier.media.all(), self.boss).filter(pk=m.pk).exists())

    def test_app_upload_source_device_visibility_and_api(self):
        token = self.paired()
        r = self.api("post", "cards_app_cards", token, data={
            "image": SimpleUploadedFile("f.jpg", jpeg(), "image/jpeg"), "uuid": "u-9", "visibility": "private"})
        j = r.json()["card"]
        m = SupplierMedia.objects.get(pk=j["id"])
        self.assertEqual((m.source, m.visibility, m.device.name), ("app", "private", "Pixel 8"))
        self.assertEqual(j["source_label"], "📱 Pixel 8")
        self.assertTrue(j["can_set_visibility"])
        boss_token = self.paired(self.boss)
        self.assertEqual(self.api("get", "cards_app_card", boss_token, m.pk).status_code, 404)
        self.assertEqual(self.api("get", "cards_app_card_image", boss_token, m.pk).status_code, 404)
        ids = [c["id"] for c in self.api("get", "cards_app_cards", boss_token).json()["items"]]
        self.assertNotIn(m.pk, ids)
        self.assertIn(self.team.pk, ids)
        # свою — у «👥 усі» з телефона; чужу — ні
        r = self.api("post", "cards_app_card", token, m.pk, data=json.dumps({"visibility": "team"}),
                     content_type="application/json")
        self.assertEqual(r.json()["card"]["visibility"], "team")
        self.api("post", "cards_app_card", token, self.team.pk, data=json.dumps({"visibility": "private"}),
                 content_type="application/json")
        self.team.refresh_from_db()
        self.assertEqual(self.team.visibility, "team")

    def test_pair_info_shows_whose_qr_and_keeps_the_code(self):
        self.user.profile.role = "admin"
        self.user.profile.save()
        qr = json.loads(card_app.new_pairing(self.user, "https://minerva.example")["qr"])
        info = self.client.post(reverse("cards_app_pair_info"), json.dumps({"code": qr["code"]}),
                                content_type="application/json").json()
        self.assertEqual(info["user"]["role"], "🔑 Адміністратор")
        self.assertEqual(info["user"]["email"], self.user.email)
        r = self.client.post(reverse("cards_app_pair"), json.dumps({"code": qr["code"], "device": "Pixel"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)                                 # код ще дійсний
        self.assertEqual(r.json()["user"]["role"], "🔑 Адміністратор")
        again = self.client.post(reverse("cards_app_pair_info"), json.dumps({"code": qr["code"]}),
                                 content_type="application/json")
        self.assertEqual(again.status_code, 410)
        self.assertEqual(self.client.post(reverse("cards_app_pair_info"), json.dumps({"code": "nope"}),
                                          content_type="application/json").status_code, 410)

    def test_public_download_page_without_login(self):
        self.client.logout()
        page = self.client.get(reverse("cards_app_download"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "⬇ Завантажити APK")
        self.assertContains(page, card_app.APP_URL)
        r = self.client.get(reverse("cards_app_apk"))
        self.assertEqual((r.status_code, r["Location"]), (302, card_app.APP_URL))
        self.client.force_login(self.user)
        app_page = self.client.get(reverse("admin:inventory_supplier_card_app")).content.decode()
        self.assertIn(reverse("cards_app_download"), app_page)
