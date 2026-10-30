"""/media/ з перевіркою доступу (tabele/media.py).

python manage.py test core.tests_media --settings=tabele.settings_test
"""
import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import TestCase, override_settings


def person(username, role="manager"):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    user.profile.save()
    return User.objects.get(pk=user.pk)


class MediaAccessTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        over = override_settings(MEDIA_ROOT=self.root)
        over.enable()
        self.addCleanup(over.disable)
        for rel, data in {"products/images/a.png": b"\x89PNG\r\n\x1a\n", "offers/A-1/Angebot.pdf": b"%PDF-1.4",
                          "orders/webshop/1/CI.pdf": b"%PDF-1.4", "labels/x.dymo": b"<xml/>",
                          "knowledge/abc/doc.pdf": b"%PDF-1.4", "orders/webshop/1/evil.html": b"<script>x</script>"
                          }.items():
            path = os.path.join(self.root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)

    def test_public_and_private(self):
        self.assertEqual(self.client.get("/media/products/images/a.png").status_code, 200)   # сайт, листи
        r = self.client.get("/media/offers/A-1/Angebot.pdf")
        self.assertEqual(r.status_code, 302)                                                  # без входу — ні
        self.assertIn("/login", r["Location"])
        self.assertEqual(self.client.get("/media/../settings.py").status_code, 404)

    def test_module_rights(self):
        self.client.force_login(person("olga"))                      # менеджер: продажі, Angebote — так
        self.assertEqual(self.client.get("/media/offers/A-1/Angebot.pdf").status_code, 200)
        self.assertEqual(self.client.get("/media/labels/x.dymo").status_code, 200)
        self.assertEqual(self.client.get("/media/knowledge/abc/doc.pdf").status_code, 404)  # база знань — сторінка
        self.client.force_login(person("kim", "warehouse"))          # складник: продажів немає
        self.assertEqual(self.client.get("/media/orders/webshop/1/CI.pdf").status_code, 404)

    def test_mail_attachments_only_owner(self):
        from email_assistant.models import EmailAccount
        olga, ivan = person("olga"), person("ivan")
        acc = EmailAccount.objects.create(user=ivan, email_address="ivan@x.example", imap_username="ivan",
                                          imap_password="x", smtp_username="ivan", smtp_password="x")
        path = os.path.join(self.root, "email_attachments", str(acc.pk), "1_abc")
        os.makedirs(path)
        with open(os.path.join(path, "0_a.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.4")
        url = f"/media/email_attachments/{acc.pk}/1_abc/0_a.pdf"
        self.client.force_login(olga)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(ivan)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))   # і адміністратор — ні
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_html_never_rendered_inline(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.y", "p"))
        r = self.client.get("/media/orders/webshop/1/evil.html")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r["Content-Disposition"], r["Content-Security-Policy"], r["X-Content-Type-Options"]),
                         ("attachment", "sandbox", "nosniff"))
