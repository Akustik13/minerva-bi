"""📱 Додаток «Minerva Візитки»: підключення QR-кодом, ключ телефону, вивантаження без дублів, список, правка,
категорії, пресети, мініатюри, відключення (suppliers/services/card_app.py, suppliers/app_api.py)."""
import io
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from suppliers.models import CardAppDevice, SupplierCardPreset, SupplierCategory, SupplierMedia
from suppliers.services import card_app
from suppliers.tests import ME, Base, png


def jpeg(color=(200, 30, 30)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (900, 560), color).save(out, "JPEG")
    return out.getvalue()


class CardAppTests(Base):
    def paired(self, user=None):
        user = user or self.user
        qr = json.loads(card_app.new_pairing(user, "https://minerva.example")["qr"])
        r = self.client.post(reverse("cards_app_pair"), json.dumps({"code": qr["code"], "device": "Pixel 8",
                                                                     "version": "1.0.0"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["token"]

    def api(self, method, name, token, *args, data=None, **kw):
        url = reverse(name, args=args)
        h = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if method == "get":
            return self.client.get(url, data or {}, **h)
        return self.client.post(url, data or {}, **h, **kw)

    def test_pairing_page_and_one_time_code(self):
        page = self.client.get(reverse("admin:inventory_supplier_card_app"))
        self.assertContains(page, "🔗 Показати QR для підключення")
        self.assertContains(page, card_app.APP_URL)
        j = self.client.post(reverse("admin:inventory_supplier_card_app"), {"action": "pair"}).json()
        qr = json.loads(j["qr"])
        self.assertEqual((qr["minerva"], qr["v"]), ("cards", 1))
        self.assertFalse(self.client.get(reverse("admin:inventory_supplier_card_app"), {"state": j["key"]}).json()["paired"])
        r = self.client.post(reverse("cards_app_pair"), {"code": qr["code"], "device": "Pixel 8"})
        token = r.json()["token"]
        self.assertTrue(token.startswith("mvc_"))
        self.assertEqual(r.json()["user"]["email"], ME)
        dev = CardAppDevice.objects.get()
        self.assertNotIn(token, dev.token_hash)                                       # у базі — лише хеш
        st = self.client.get(reverse("admin:inventory_supplier_card_app"), {"state": j["key"]}).json()
        self.assertEqual(st, {"paired": True, "device": "Pixel 8"})
        again = self.client.post(reverse("cards_app_pair"), {"code": qr["code"], "device": "X"})
        self.assertEqual(again.status_code, 410)                                    # одноразовий
        self.assertEqual(self.client.post(reverse("cards_app_pair"), {"code": "nope"}).status_code, 410)
        self.assertEqual(CardAppDevice.objects.count(), 1)

    def test_auth_and_revoke(self):
        token = self.paired()
        self.assertEqual(self.api("get", "cards_app_me", token).json()["device"]["name"], "Pixel 8")
        self.assertEqual(self.api("get", "cards_app_me", "mvc_wrong").status_code, 401)
        self.assertEqual(self.client.get(reverse("cards_app_me")).status_code, 401)          # сесія не діє
        dev = CardAppDevice.objects.get()
        self.client.post(reverse("admin:inventory_supplier_card_app"), {"action": "revoke", "id": dev.pk})
        self.assertEqual(self.api("get", "cards_app_me", token).status_code, 401)
        other_dev = CardAppDevice.objects.create(user=self.other, name="Olga", token_hash="x" * 64)
        r = self.client.post(reverse("admin:inventory_supplier_card_app"), {"action": "revoke", "id": other_dev.pk})
        self.assertEqual(r.status_code, 302)                                    # адміністратор — будь-який телефон
        self.client.force_login(self.other)
        self.other.profile.role = "readonly"
        self.other.profile.save()
        self.assertNotEqual(self.client.get(reverse("admin:inventory_supplier_card_app")).status_code, 200)
        qr = json.loads(card_app.new_pairing(self.other, "https://m.example")["qr"])     # без права додавати — ні
        self.assertEqual(self.client.post(reverse("cards_app_pair"), {"code": qr["code"]}).status_code, 403)

    def test_upload_idempotent_with_qr_and_ai(self):
        token = self.paired()
        cat = SupplierCategory.objects.create(name="Кабелі", icon="🔌")
        data = {"uuid": "a1b2", "kind": "card", "event": "electronica 2026", "note": "стенд B3",
                "marks": "fair,star,bogus", "categories": str(cat.pk), "read_ai": "1",
                "qr_front": "BEGIN:VCARD\nVERSION:3.0\nFN:Wendy Wu\nORG:Cable Tech\nEMAIL:wendy@cable.tech\nEND:VCARD",
                "image": SimpleUploadedFile("f.jpg", jpeg()), "image_back": SimpleUploadedFile("b.jpg", jpeg((0, 0, 200)))}
        with mock.patch("suppliers.services.ai.card") as card:
            r = self.api("post", "cards_app_cards", token, data=data)
        self.assertEqual(r.status_code, 201, r.content)
        c = r.json()["card"]
        self.assertEqual((c["event"], c["note"], c["marks"]), ("electronica 2026", "стенд B3", ["fair", "star"]))
        self.assertEqual((c["company"], c["person"], c["email"]), ("Cable Tech", "Wendy Wu", "wendy@cable.tech"))
        self.assertEqual(c["own_categories"], [cat.pk])
        self.assertTrue(c["has_back"])
        card.assert_called_once()                                                    # 🧠 AI запущено
        again = self.api("post", "cards_app_cards", token, data={"uuid": "a1b2",
                                                                 "image": SimpleUploadedFile("f.jpg", jpeg())})
        self.assertEqual((again.status_code, again.json()["created"]), (200, False))
        self.assertEqual(SupplierMedia.objects.count(), 1)
        bad = self.api("post", "cards_app_cards", token, data={"uuid": "z", "image": SimpleUploadedFile("x.jpg", b"no")})
        self.assertEqual(bad.status_code, 400)
        m = SupplierMedia.objects.get()
        self.assertEqual((m.created_by, m.client_uuid), (self.user, "a1b2"))

    def test_list_filters_edit_and_image(self):
        token = self.paired()
        m = SupplierMedia.objects.create(created_by=self.user, event="Electronica 2026", marks=["fair"],
                                         extracted={"name": "PCBWay", "contact_name": "Anna Li"},
                                         cost_usd=Decimal("0.01"), state=SupplierMedia.READ)
        m.image.save("c.jpg", SimpleUploadedFile("c.jpg", jpeg()), save=True)
        n = SupplierMedia.objects.create(created_by=self.other, event="")
        n.image.save("d.png", SimpleUploadedFile("d.png", png()), save=True)
        items = self.api("get", "cards_app_cards", token).json()["items"]
        self.assertEqual([i["id"] for i in items], [n.pk, m.pk])
        self.assertEqual([i["id"] for i in self.api("get", "cards_app_cards", token, data={"q": "anna"}).json()["items"]],
                         [m.pk])
        self.assertEqual([i["id"] for i in self.api("get", "cards_app_cards", token, data={"ai": "none"}).json()["items"]],
                         [n.pk])
        self.assertEqual([i["id"] for i in self.api("get", "cards_app_cards", token,
                                                    data={"event": "electronica 2026"}).json()["items"]], [m.pk])
        self.assertEqual([i["id"] for i in self.api("get", "cards_app_cards", token,
                                                    data={"ids": f"{m.pk},999"}).json()["items"]], [m.pk])
        r = self.api("post", "cards_app_card", token, m.pk, data=json.dumps({"note": "зразки 50 шт",
                                                                             "marks": ["sample"]}),
                     content_type="application/json")
        self.assertEqual(r.json()["card"]["note"], "зразки 50 шт")
        m.refresh_from_db()
        self.assertEqual(m.marks, ["sample"])
        img = self.api("get", "cards_app_card_image", token, m.pk)
        self.assertEqual((img.status_code, img["Content-Type"]), (200, "image/jpeg"))
        full = self.api("get", "cards_app_card_image", token, m.pk, data={"size": "full"})
        self.assertEqual(full.status_code, 200)
        self.other.profile.role = "warehouse"
        self.other.profile.save()
        t2 = self.paired(self.other)                                  # чужа візитка без права змінювати — ні
        with mock.patch.object(card_app, "can", lambda user, op: op in ("add", "view")):
            r = self.api("post", "cards_app_card", t2, m.pk, data={"note": "x"})
            self.assertEqual(r.status_code, 403)
            self.assertEqual(self.api("post", "cards_app_card", t2, n.pk, data={"note": "моя"}).status_code, 200)

    def test_meta_categories_and_presets(self):
        token = self.paired()
        meta = self.api("get", "cards_app_meta", token).json()
        self.assertTrue(meta["perms"]["upload"])
        self.assertIn(["fair", "🎪 З виставки"], meta["marks"])
        r = self.api("post", "cards_app_categories", token, data={"name": "Виробники кварців", "icon": "💎"})
        cat = r.json()["category"]
        self.assertEqual(cat["icon"], "💎")
        self.assertEqual(self.api("post", "cards_app_categories", token, data={"name": "виробники кварців"}).status_code,
                         400)                                                         # та сама назва
        r = self.api("post", "cards_app_category", token, cat["id"], data={"description": "Quartz"})
        self.assertEqual(r.json()["category"]["description"], "Quartz")
        r = self.api("post", "cards_app_presets", token, data={"action": "save", "name": "electronica 2026",
                                                               "event": "electronica 2026, B3", "marks": "fair",
                                                               "categories": str(cat["id"]), "read_ai": "1",
                                                               "make_default": "1"})
        out = r.json()
        self.assertEqual(out["default"], out["id"])
        p = SupplierCardPreset.objects.get()
        self.assertEqual((p.event, p.marks, p.read_ai), ("electronica 2026, B3", ["fair"], True))
        self.assertEqual(self.api("get", "cards_app_meta", token).json()["default_preset"], p.pk)
        self.assertEqual(self.api("get", "cards_app_meta", token).json()["events"], [])

    def test_to_supplier_and_delete(self):
        token = self.paired()
        m = SupplierMedia.objects.create(created_by=self.user, extracted={"name": "Quartz Co", "email": "info@quartz.example"})
        m.image.save("c.jpg", SimpleUploadedFile("c.jpg", jpeg()), save=True)
        r = self.api("post", "cards_app_card_to_supplier", token, m.pk).json()
        self.assertTrue(r["created"])
        self.assertEqual(r["supplier"]["name"], "Quartz Co")
        self.assertEqual(self.api("post", "cards_app_card_delete", token, m.pk).json(), {"ok": True})
        self.assertFalse(SupplierMedia.objects.filter(pk=m.pk).exists())
        self.assertEqual(self.api("get", "cards_app_card_delete", token, m.pk).status_code, 405)
