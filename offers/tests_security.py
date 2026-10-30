"""Angebote: надіслана версія не «розблоковується» списком статусів; дії картки, легка форма й лист з Angebot —
лише з правами. python manage.py test offers.tests_security --settings=tabele.settings_test"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from email_assistant.models import EmailAccount
from offers import services
from offers.models import Offer


def person(username, role="readonly", **profile):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


class Base(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        self.boss = User.objects.create_superuser("slava", "s@x.y", "p")
        self.offer = Offer.objects.create(contact_name="Alex", status=Offer.SENT)
        # лише перегляд Angebote: модуль відкрито, операцій змінювати немає (роль «лише перегляд»)
        self.viewer = person("vika", allowed_modules=["offers", "email_assistant"])
        EmailAccount.objects.create(user=self.viewer, email_address="vika@sevskiy.example", imap_username="v",
                                    imap_password="x", smtp_username="v", smtp_password="x", is_primary=True)


class LockedStatusTests(Base):
    def post_status(self, status):
        self.client.force_login(self.boss)
        return self.client.post(f"/admin/offers/offer/{self.offer.pk}/change/", {
            "status": status, "internal_notes": "",
            "lines-TOTAL_FORMS": "0", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "1000"})

    def test_sent_offer_cannot_go_back_to_draft(self):
        for status in (Offer.DRAFT, Offer.REVIEW, Offer.APPROVED, Offer.ACCEPTED):
            self.post_status(status)
            self.offer.refresh_from_db()
            self.assertEqual(self.offer.status, Offer.SENT, status)
            self.assertTrue(self.offer.is_locked)

    def test_allowed_next_states(self):
        self.post_status(Offer.REJECTED)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, Offer.REJECTED)
        self.post_status(Offer.DRAFT)
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, Offer.REJECTED)        # назад — лише «♻ Нова версія»

    def test_dropdown_lists_only_allowed(self):
        self.client.force_login(self.boss)
        form = self.client.get(f"/admin/offers/offer/{self.offer.pk}/change/").context["adminform"].form
        self.assertEqual([c for c, _ in form.fields["status"].choices],
                         [Offer.SENT, Offer.REJECTED, Offer.EXPIRED, Offer.CANCELLED])
        self.assertEqual(services.manual_statuses(Offer.ACCEPTED), (Offer.ACCEPTED,))
        self.assertNotIn(Offer.SENT, services.manual_statuses(Offer.DRAFT))

    def test_new_version_still_unlocks(self):
        services.new_version(self.offer)
        self.offer.refresh_from_db()
        self.assertEqual((self.offer.status, self.offer.revision), (Offer.DRAFT, 2))


class PermissionTests(Base):
    def test_card_actions_need_change_permission(self):
        self.client.force_login(self.viewer)
        for action in ("accept", "reject", "new-version", "generate", "approve", "review"):
            r = self.client.post(f"/admin/offers/offer/{self.offer.pk}/{action}/")
            self.assertEqual(r.status_code, 403, action)
        self.offer.refresh_from_db()
        self.assertEqual((self.offer.status, self.offer.order_id, self.offer.revision), (Offer.SENT, None, 1))

    def test_quick_form_needs_view_permission(self):
        clerk = person("wh", "warehouse")                           # без модуля «Angebote»
        self.client.force_login(clerk)
        self.assertEqual(self.client.get(f"/offers/{self.offer.pk}/quick/").status_code, 403)
        self.assertEqual(self.client.get("/offers/quick/").status_code, 403)
        self.assertEqual(self.client.get("/offers/api/price/?sku=X&qtys=1").status_code, 403)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(f"/offers/{self.offer.pk}/quick/").status_code, 200)

    def test_compose_with_offer_does_not_generate_without_change_permission(self):
        self.client.force_login(self.viewer)
        with patch("offers.services.generate_document") as gen:
            r = self.client.get(f"/email/compose/?offer={self.offer.pk}")
        self.assertEqual(r.status_code, 200)
        gen.assert_not_called()                                     # GET без права змінювати — без нового документа
        self.assertEqual(r.context["initial"]["att_refs"], [])      # документа ще немає — нічого не вкладено
        clerk = person("wh2", "warehouse", allowed_modules=["email_assistant"])
        EmailAccount.objects.create(user=clerk, email_address="wh2@sevskiy.example", imap_username="w",
                                    imap_password="x", smtp_username="w", smtp_password="x", is_primary=True)
        self.client.force_login(clerk)
        self.assertEqual(self.client.get(f"/email/compose/?offer={self.offer.pk}").status_code, 403)
