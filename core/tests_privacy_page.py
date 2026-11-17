"""Публічна сторінка конфіденційності /privacy/ (адреса для застосунку Google OAuth):
python manage.py test core.tests_privacy_page --settings=tabele.settings_test"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from config.models import SystemSettings


class PrivacyPageTests(TestCase):
    def setUp(self):
        s = SystemSettings.get()
        s.company_name, s.company_email = "Muster GmbH", "info@muster.example"
        s.save()

    def test_public_without_login(self):
        r = self.client.get("/privacy/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Muster GmbH")
        self.assertContains(r, "mailto:info@muster.example")
        self.assertContains(r, "https://www.googleapis.com/auth/calendar")
        self.assertContains(r, "Limited Use")
        self.assertContains(r, 'id="de"')

    def test_staff_sees_it_too_and_no_post(self):
        user = get_user_model().objects.create_user("anna", "a@x.y", "p", is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.get("/privacy/").status_code, 200)   # не редірект в адмінку, як «/»
        self.assertEqual(self.client.post("/privacy/").status_code, 405)
