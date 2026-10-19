"""
Тести пошти ↔ CRM.  Запуск:  python manage.py test email_assistant --settings=tabele.settings_test
"""
import email as email_lib
from datetime import datetime, timezone as dt_tz
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from crm.models import Customer, CustomerTimeline
from email_assistant.crm_link import find_order, resolve, own_addresses
from email_assistant.imap_client import _get_body, html_to_text, parse_addresses
from email_assistant.models import EmailAccount, EmailMessage
from sales.models import SalesOrder

# Лист з веб-пошти IONOS/OX: лише text/html, без multipart
WEBMAIL_RAW = (
    b'From: Viacheslav Pryimak <viacheslav.pryimak@sevskiy.de>\r\n'
    b'To: "Threatts, Travis" <travis@example.com>\r\n'
    b'Cc: sergey@sevskiy.de\r\n'
    b'Subject: UPS brokerage fee\r\n'
    b'Message-ID: <ox.1@sevskiy.de>\r\n'
    b'Content-Type: text/html; charset=utf-8\r\n'
    b'\r\n'
    b'<html><body><div style="font-family: verdana"><p>Dear Travis,</p>'
    b'<p>Your shipment for <strong>DigiKey order 99823894</strong> is on hold.</p>'
    b'<p>Best regards,<br>Viacheslav</p></div></body></html>'
)


class BodyParsingTests(TestCase):
    def test_html_only_message_goes_to_body_html(self):
        msg = email_lib.message_from_bytes(WEBMAIL_RAW)
        text, html = _get_body(msg)
        self.assertIn('<strong>DigiKey order 99823894</strong>', html)
        self.assertNotIn('<', text)
        self.assertIn('Dear Travis,', text)
        self.assertIn('Best regards,\nViacheslav', text)

    def test_html_to_text_drops_styles(self):
        self.assertEqual(html_to_text('<style>p{color:red}</style><p>A&amp;B</p>'), 'A&B')

    def test_addresses_with_comma_in_name(self):
        self.assertEqual(parse_addresses('"Threatts, Travis" <Travis@Example.com>, b@x.de'),
                         ['travis@example.com', 'b@x.de'])


class CrmLinkTestBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(
            user=self.user, email_address='viacheslav.pryimak@sevskiy.de',
            imap_username='viacheslav.pryimak@sevskiy.de', imap_password='x',
            smtp_username='viacheslav.pryimak@sevskiy.de', smtp_password='x',
            imap_folder_inbox='INBOX', imap_folder_sent='Gesendete Objekte')
        self.travis = Customer.objects.create(name='TRAVIS THREATTS', email='travis@example.com')
        self.ida = Customer.objects.create(name='IDA FEJZIC', email='ifejzic@example.com')
        # «Клієнт» з нашою ж адресою (тестові/внутрішні замовлення) — не має перехоплювати листи
        Customer.objects.create(name='Sevskiy GmbH', email='viacheslav.pryimak@sevskiy.de')
        self.order1 = self._order('99681836', self.travis)
        self.order2 = self._order('99823894', self.travis)
        self.ida_order = self._order('102009980', self.ida)

    def _order(self, number, customer):
        return SalesOrder.objects.create(
            source='digikey', order_number=number, customer_key=customer.external_key,
            email=customer.email, affects_stock=False)

    def _message(self, **kw):
        data = dict(
            account=self.account, folder=EmailMessage.FOLDER_INBOX, imap_folder_name='INBOX',
            subject='', from_email='', to_emails=[], cc_emails=[], body_text='',
            sent_at=datetime(2026, 10, 5, 12, tzinfo=dt_tz.utc),
        )
        data.update(kw)
        return EmailMessage.objects.create(**data)

    def _mail_events(self, customer):
        return CustomerTimeline.objects.filter(customer=customer, event_type__in=['email_in', 'email_out'])


class ResolveTests(CrmLinkTestBase):
    def test_own_cc_copy_in_inbox_is_outgoing(self):
        customer, direction, order = resolve(
            from_email='viacheslav.pryimak@sevskiy.de', to_emails=['travis@example.com'],
            cc_emails=['sergey@sevskiy.de'], subject='Your order #99823894 has been shipped')
        self.assertEqual((customer, direction, order), (self.travis, 'out', self.order2))

    def test_incoming_from_customer(self):
        customer, direction, _ = resolve(
            from_email='travis@example.com', to_emails=['viacheslav.pryimak@sevskiy.de'], subject='Hi')
        self.assertEqual((customer, direction), (self.travis, 'in'))

    def test_internal_mail_is_not_linked(self):
        customer, _, _ = resolve(
            from_email='sergey@sevskiy.de', to_emails=['viacheslav.pryimak@sevskiy.de'],
            subject='Order 99823894')
        self.assertIsNone(customer)

    def test_order_number_of_other_customer_in_body(self):
        order = find_order('UPS fee', 'Shipment for DigiKey order 102009980 is on hold', self.travis)
        self.assertEqual(order, self.ida_order)

    def test_unknown_address_falls_back_to_order_customer(self):
        customer, _, order = resolve(
            from_email='viacheslav.pryimak@sevskiy.de', to_emails=['purchasing@example.org'],
            subject='Your order #102009980 has been shipped')
        self.assertEqual((customer, order), (self.ida, self.ida_order))

    def test_own_domains_exclude_free_mail(self):
        EmailAccount.objects.create(user=self.user, email_address='me@gmail.com',
                                    imap_username='me@gmail.com', imap_password='x',
                                    smtp_username='me@gmail.com', smtp_password='x')
        addrs, domains = own_addresses()
        self.assertIn('me@gmail.com', addrs)
        self.assertNotIn('gmail.com', domains)
        self.assertIn('sevskiy.de', domains)


class SignalLinkTests(CrmLinkTestBase):
    def test_webmail_sent_message_lands_in_crm_once(self):
        sent = self._message(
            folder=EmailMessage.FOLDER_SENT, imap_folder_name='Gesendete Objekte', imap_uid=80,
            message_id='<ox.1@sevskiy.de>', subject='UPS brokerage fee',
            from_email='viacheslav.pryimak@sevskiy.de',
            to_emails=['travis@example.com'], cc_emails=['sergey@sevskiy.de'],
            body_text='Your shipment for DigiKey order 99823894 is on hold.')
        events = self._mail_events(self.travis)
        self.assertEqual(events.count(), 1)
        ev = events.get()
        self.assertEqual((ev.event_type, ev.email_message, ev.related_order_id),
                         ('email_out', sent, self.order2.pk))

        # Та сама копія прийшла у Вхідні через CC — нової події немає
        self._message(
            imap_uid=500, message_id='<ox.1@sevskiy.de>', subject='UPS brokerage fee',
            from_email='viacheslav.pryimak@sevskiy.de', to_emails=['travis@example.com'],
            cc_emails=['sergey@sevskiy.de'], body_text='...')
        self.assertEqual(self._mail_events(self.travis).count(), 1)
        self.assertFalse(CustomerTimeline.objects.filter(customer__name='Sevskiy GmbH').exists())

    def test_spam_is_not_linked(self):
        self._message(folder=EmailMessage.FOLDER_SPAM, is_spam=True, imap_uid=7,
                      from_email='travis@example.com', subject='spam')
        self.assertFalse(self._mail_events(self.travis).exists())


class RelinkCommandTests(CrmLinkTestBase):
    def test_repairs_legacy_events_and_html(self):
        # Старий стан: лист у Вхідних (наша CC-копія) збережено як «отриманий»,
        # копія з «Надісланих» — окремою подією; HTML-only лист — сирим HTML у тексті.
        CustomerTimeline.objects.all().delete()
        EmailMessage.objects.bulk_create([
            EmailMessage(account=self.account, folder='inbox', imap_folder_name='INBOX', imap_uid=500,
                         message_id='<n1@sevskiy.de>', subject='Your order #99681836 has been shipped',
                         from_email='viacheslav.pryimak@sevskiy.de', to_emails=['travis@example.com'],
                         body_text='Dear TRAVIS', sent_at=datetime(2026, 6, 9, tzinfo=dt_tz.utc)),
            EmailMessage(account=self.account, folder='sent', imap_folder_name='Gesendete Objekte',
                         imap_uid=80, message_id='<n1@sevskiy.de>',
                         subject='Your order #99681836 has been shipped',
                         from_email='viacheslav.pryimak@sevskiy.de', to_emails=['travis@example.com'],
                         body_text='Dear TRAVIS', sent_at=datetime(2026, 6, 9, tzinfo=dt_tz.utc)),
            EmailMessage(account=self.account, folder='sent', imap_folder_name='Gesendete Objekte',
                         imap_uid=81, message_id='<ox.1@sevskiy.de>', subject='UPS brokerage fee',
                         from_email='viacheslav.pryimak@sevskiy.de', to_emails=['travis@example.com'],
                         body_text=WEBMAIL_RAW.split(b'\r\n\r\n', 1)[1].decode(),
                         sent_at=datetime(2026, 10, 5, tzinfo=dt_tz.utc)),
        ])  # bulk_create — без сигналів, як дані до оновлення
        for event_type, uid in (('email_in', 500), ('email_out', 80)):
            CustomerTimeline.objects.create(
                customer=self.travis, user=self.user, event_type=event_type,
                title='Your order #99681836 has been shipped', related_email_id=uid)

        out = StringIO()
        call_command('relink_email_crm', '--dry-run', stdout=out)
        self.assertIn('[DRY-RUN]', out.getvalue())
        self.assertEqual(self._mail_events(self.travis).count(), 2)  # нічого не змінено

        call_command('relink_email_crm', stdout=StringIO())
        events = {e.title: e for e in self._mail_events(self.travis)}
        self.assertEqual(len(events), 2)
        shipped = events['Your order #99681836 has been shipped']
        self.assertEqual((shipped.event_type, shipped.related_order_id), ('email_out', self.order1.pk))
        ups = events['UPS brokerage fee']
        self.assertEqual((ups.event_type, ups.related_order_id), ('email_out', self.order2.pk))
        self.assertNotIn('<div', ups.body)

        fixed = EmailMessage.objects.get(imap_uid=81)
        self.assertTrue(fixed.body_html.startswith('<html>'))
        self.assertIn('Dear Travis,', fixed.body_text)


class TimelineApiTests(CrmLinkTestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def test_old_events_get_order_from_title(self):
        CustomerTimeline.objects.create(customer=self.travis, user=self.user, event_type='email_in',
                                        title='Your order #99823894 has been received')
        CustomerTimeline.objects.create(customer=self.travis, user=self.user, event_type='note',
                                        title='Called about order 99823894')
        r = self.client.get(f'/crm/customer/{self.travis.pk}/timeline/')
        self.assertEqual(r.status_code, 200)
        by_title = {e['title']: e for e in r.json()['events']}
        mail = by_title['Your order #99823894 has been received']
        self.assertEqual(mail['order']['number'], '99823894')
        self.assertIn(f'/sales/salesorder/{self.order2.pk}/', mail['order']['url'])
        self.assertEqual(mail['thread'], 'your order #99823894 has been received')
        self.assertIsNone(by_title['Called about order 99823894']['order'])  # нотатки не групуються
        order_event = by_title['Замовлення #99823894']  # подія з сигналу створення замовлення
        self.assertEqual(order_event['order']['id'], self.order2.pk)
