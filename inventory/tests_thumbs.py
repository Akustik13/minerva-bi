"""Мініатюри фото, 📄 даташит і фільтр «Фото / даташит» у списку товарів та огляді складу.
python manage.py test inventory --settings=tabele.settings_test"""
import shutil
import tempfile
from io import BytesIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from inventory import thumbs
from inventory.models import Product
from inventory.services.media_files import MediaError


def picture(fmt="JPEG", size=(400, 300), mode="RGB") -> bytes:
    from PIL import Image
    out = BytesIO()
    Image.new(mode, size, (200, 30, 30, 128) if mode == "RGBA" else (200, 30, 30)).save(out, fmt)
    return out.getvalue()


class ThumbTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        o = override_settings(MEDIA_ROOT=self.tmp)
        o.enable()
        self.addCleanup(o.disable)
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(User.objects.create_superuser("adm", "adm@example.com", "p"))
        self.file = Product.objects.create(sku="AN-FILE", name="With file", datasheet_url="https://docs.example.com/AN-FILE.pdf")
        self.file.image.save("AN-FILE.php", ContentFile(picture()), save=True)   # стара назва .php — вміст JPEG
        self.url = Product.objects.create(sku="AN-URL", name="With link", image_url="https://img.example.com/AN-URL.png")
        self.none = Product.objects.create(sku="AN-NONE", name="Nothing")

    def test_render_and_cache(self):
        from PIL import Image
        data = thumbs.render(picture("PNG", (500, 200), "RGBA"))
        with Image.open(BytesIO(data)) as im:
            self.assertEqual((im.format, max(im.size)), ("JPEG", thumbs.SIZE))
        first = thumbs.get(self.file)
        self.assertTrue(first and first[:3] == b"\xff\xd8\xff")
        with mock.patch.object(thumbs, "render") as again:
            self.assertEqual(thumbs.get(self.file), first)   # з кешу
            again.assert_not_called()
        self.assertIsNone(thumbs.get(self.none))

    def test_link_fetched_once_and_failures_remembered(self):
        with mock.patch.object(thumbs, "fetch", return_value=picture("PNG")) as fetch:
            self.assertIsNotNone(thumbs.get(self.url))
            self.assertIsNotNone(thumbs.get(self.url))
        self.assertEqual(fetch.call_count, 1)
        self.url.image_url = "https://img.example.com/missing.png"
        self.url.save()
        with mock.patch.object(thumbs, "fetch", side_effect=MediaError("сервер відповів HTTP 404")) as fetch:
            self.assertIsNone(thumbs.get(self.url))
            self.assertIsNone(thumbs.get(self.url))   # годину не пробує знову
        self.assertEqual(fetch.call_count, 1)

    def test_list_column_filter_and_thumb_view(self):
        r = self.client.get("/admin/inventory/product/")
        self.assertContains(r, 'class="inv-thumb"')
        self.assertContains(r, thumbs.thumb_url(self.file).replace("&", "&amp;"))
        self.assertContains(r, "Немає даташита")
        self.assertContains(r, "https://docs.example.com/AN-FILE.pdf")
        for value, expected in [("no_image", {"AN-NONE"}), ("no_ds", {"AN-URL", "AN-NONE"}),
                                ("no_both", {"AN-NONE"}), ("image", {"AN-FILE", "AN-URL"}), ("ds", {"AN-FILE"})]:
            r = self.client.get(f"/admin/inventory/product/?media={value}")
            shown = {p.sku for p in r.context["cl"].result_list}
            self.assertEqual(shown, expected, value)
        r = self.client.get(thumbs.thumb_url(self.file))
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/jpeg"))
        self.assertEqual(self.client.get(f"/admin/inventory/product/{self.none.pk}/thumb/").status_code, 404)

    def test_supply_page(self):
        r = self.client.get("/admin/inventory/reorderproxy/supply/")
        self.assertContains(r, "svFilter('noimg', this)")
        self.assertContains(r, 'class="sv-thumb"')
        self.assertContains(r, 'data-has-ds="1"')
        self.assertContains(r, 'class="sv-ds"')
