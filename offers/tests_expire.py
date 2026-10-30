"""Прострочені Angebote — щодня з cron (expire_offers), а не лише при відкритті списку.

    python manage.py test offers --settings=tabele.settings_test
"""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from offers import services
from offers.models import Offer


class ExpireOffersCommandTests(TestCase):
    def offer(self, status, days):
        o = services.new_offer(contact_name="Max")
        Offer.objects.filter(pk=o.pk).update(
            status=status, valid_until=timezone.localdate() + datetime.timedelta(days=days))
        return o

    def test_expires_only_sent_past_validity(self):
        old = self.offer(Offer.SENT, -1)
        today = self.offer(Offer.SENT, 0)
        draft = self.offer(Offer.DRAFT, -10)
        accepted = self.offer(Offer.ACCEPTED, -10)
        out = StringIO()
        call_command("expire_offers", stdout=out)
        self.assertIn("прострочено 1", out.getvalue())
        status = dict(Offer.objects.values_list("pk", "status"))
        self.assertEqual(status[old.pk], Offer.EXPIRED)
        self.assertEqual(status[today.pk], Offer.SENT)      # діє до кінця дня valid_until
        self.assertEqual(status[draft.pk], Offer.DRAFT)
        self.assertEqual(status[accepted.pk], Offer.ACCEPTED)

        out = StringIO()
        call_command("expire_offers", stdout=out)           # повтор нічого не змінює й мовчить
        self.assertEqual(out.getvalue(), "")
