"""🎙 Наради в CRM: «🎙 Записати нараду» в картці клієнта (object-tools, поза формою картки) і дія панелі праворуч
(за замовчуванням вимкнена) — лише тому, хто може записувати. У хронологію клієнта наради нічого не пишуть.

    python manage.py test crm.tests_meetings_ui --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.utils import timezone

from core import side_panel as sp
from crm.models import Customer, CustomerTimeline
from meetings.models import Meeting, MeetingSettings


def enable_recording(on=True):
    s = MeetingSettings.load()
    s.enabled = on
    s.texts_reviewed_at = timezone.now() if on else None
    s.save()


def person(username, role):
    user = User.objects.create_user(username, f"{username}@x.y", "p", is_staff=True)
    user.profile.role = role
    user.profile.save()
    return User.objects.get(pk=user.pk)


class CustomerMeetingTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={"is_onboarding_complete": True})
        enable_recording()
        self.boss = User.objects.create_superuser("boss", "boss@x.y", "p")
        self.customer = Customer.objects.create(name="Muster GmbH", email="einkauf@muster.example")

    def card(self, user):
        self.client.force_login(user)
        r = self.client.get(f"/admin/crm/customer/{self.customer.pk}/change/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_card_button_outside_the_form(self):
        html = self.card(self.boss)
        url = f"/admin/meetings/meeting/new/?customer={self.customer.pk}"
        self.assertIn(url, html)
        self.assertLess(html.index(url), html.index('id="customer_form"'))    # object-tools — поза формою картки

    def test_no_button_without_meetings_right(self):
        self.assertNotIn("/admin/meetings/meeting/new/", self.card(person("acc", "ai")))   # роль без нарад

    def test_side_panel_action_off_by_default_and_gated(self):
        panel = sp.get("crm.customer")
        action = next(a for a in panel.actions if a.key == "meeting")
        self.assertFalse(action.default)
        request = RequestFactory().get("/")
        request.user = self.boss
        self.assertIn("meeting", [a.key for a in sp.visible_actions(panel, request, self.customer)])
        self.assertEqual(action.href(self.customer, request), f"/admin/meetings/meeting/new/?customer={self.customer.pk}")
        enable_recording(False)
        self.assertNotIn("meeting", [a.key for a in sp.visible_actions(panel, request, self.customer)])
        request.user = person("acc2", "ai")
        enable_recording()
        self.assertNotIn("meeting", [a.key for a in sp.visible_actions(panel, request, self.customer)])

    def test_meeting_never_reaches_customer_timeline(self):
        before = CustomerTimeline.objects.filter(customer=self.customer).count()
        Meeting.objects.create(owner=self.boss, title="Jahresgespräch", customer=self.customer)
        self.assertEqual(CustomerTimeline.objects.filter(customer=self.customer).count(), before)
        self.assertNotIn("Jahresgespräch", self.card(self.boss))
