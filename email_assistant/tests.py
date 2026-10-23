"""
Тести пошти ↔ CRM.  Запуск:  python manage.py test email_assistant --settings=tabele.settings_test
"""
import email as email_lib
import json
from datetime import datetime, timedelta, timezone as dt_tz
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from crm.models import Customer, CustomerTimeline
from email_assistant.crm_link import find_order, resolve, own_addresses
from email_assistant.imap_client import IMAPClient, _get_body, decode_mutf7, html_to_text, parse_addresses
from email_assistant.models import EmailAccount, EmailCategoryRule, EmailMessage, EmailSettings, EmailThread
from email_assistant.sync import folders_to_sync, pull_read_flags, set_read, sync_folder
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


# ── Синхронізація папок і прапорця «прочитано» ───────────────────────────────

class FakeListConn:
    def list(self):
        return 'OK', [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Sent) "/" "Gesendete Objekte"',
            b'(\\HasChildren \\Noselect) "/" "Projekte"',
            b'(\\HasNoChildren) "/" "Projekte/Entw&APw-rfe"',
            b'(\\HasNoChildren) NIL Flat',
            (b'(\\HasNoChildren) "/" {10}', b'Say "hi" x'),
            b'',
        ]


class FolderListTests(TestCase):
    def test_list_folders_handles_nil_literal_and_utf7(self):
        client = IMAPClient.__new__(IMAPClient)
        client.conn = FakeListConn()
        folders = {f['name']: f for f in client.list_folders()}
        self.assertEqual(folders['Projekte/Entw&APw-rfe']['label'], 'Entwürfe')
        self.assertEqual(folders['Projekte/Entw&APw-rfe']['level'], 1)
        self.assertFalse(folders['Projekte']['selectable'])
        self.assertIn('Flat', folders)                      # роздільник NIL
        self.assertIn('Say "hi" x', folders)                # назва-літерал
        self.assertEqual(folders['Gesendete Objekte']['special'], '\\Sent')
        synced = [f['name'] for f in folders_to_sync(folders.values())]
        self.assertEqual(sorted(synced), ['Flat', 'Projekte/Entw&APw-rfe', 'Say "hi" x'])

    def test_decode_mutf7(self):
        self.assertEqual(decode_mutf7('Entw&APw-rfe &-'), 'Entwürfe &')


def _msg(uid, subject='Hello', seen=False, from_email='partner@example.org'):
    return {
        'uid': uid, 'message_id': f'<m{uid}@example.org>', 'in_reply_to': '',
        'subject': f'{subject} {uid}', 'from_email': from_email, 'from_name': '',
        'to_emails': ['viacheslav.pryimak@sevskiy.de'], 'cc_emails': [],
        'body_text': 'text', 'body_html': '', 'attachments': [],
        'is_read': seen, 'sent_at': datetime(2026, 10, 1, 9, uid % 60, tzinfo=dt_tz.utc),
    }


class FakeIMAP:
    """Сервер у пам'яті: {папка: {uid: прочитано?}}; записує виклики set_seen."""

    def __init__(self, folders):
        self.folders = folders
        self.stored = []
        self._selected = None

    def __call__(self, account):      # підміна класу IMAPClient(account)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def list_folders(self):
        return [{'name': n, 'label': n, 'path': n, 'selectable': True, 'level': 0, 'special': ''}
                for n in self.folders]

    def search_uids_after(self, folder, after_uid=0):
        if folder not in self.folders:
            return None
        self._selected = folder
        return [str(u).encode() for u in sorted(self.folders[folder]) if u > after_uid]

    def fetch_by_uids_iter(self, uids):
        for u in uids:
            uid = int(u)
            yield _msg(uid, seen=self.folders[self._selected][uid])

    def fetch_messages(self, folder, days_back=30, since_uid=0, limit=None):
        return [_msg(u, seen=seen) for u, seen in sorted(self.folders.get(folder, {}).items()) if u > since_uid]

    def search_unseen(self, folder):
        if folder not in self.folders:
            return None
        return {u for u, seen in self.folders[folder].items() if not seen}

    def set_seen(self, folder, uids, seen=True):
        self.stored.append((folder, sorted(uids), seen))
        for u in uids:
            self.folders[folder][u] = seen
        return True


class SyncTestBase(TestCase):
    FOLDER = 'Projekte/Entw&APw-rfe'

    def setUp(self):
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(
            user=self.user, email_address='viacheslav.pryimak@sevskiy.de',
            imap_username='viacheslav.pryimak@sevskiy.de', imap_password='x',
            smtp_username='viacheslav.pryimak@sevskiy.de', smtp_password='x',
            imap_folder_inbox='INBOX', imap_folder_sent='Gesendete Objekte')
        es = EmailSettings.get_for_user(self.user)
        es.deadline_detection = False
        es.telegram_notify_new = False
        es.mark_read_on_server = False   # без справжніх IMAP-підключень у фоні; тести «прочитано» вмикають самі
        es.save()

    def _push_read_on(self):
        es = EmailSettings.get_for_user(self.user)
        es.mark_read_on_server = True
        es.save()


class SyncFolderTests(SyncTestBase):
    def test_incremental_then_full(self):
        server = FakeIMAP({self.FOLDER: {1: True, 2: False, 3: False}})
        new = sync_folder(server, self.account, self.FOLDER, limit=2)
        self.assertEqual(sorted(m.imap_uid for m in new), [2, 3])   # перший раз — останні N, без вікна в днях

        server.folders[self.FOLDER][4] = False
        new = sync_folder(server, self.account, self.FOLDER, limit=2)
        self.assertEqual([m.imap_uid for m in new], [4])            # далі — лише новіші

        new = sync_folder(server, self.account, self.FOLDER, full=True)
        self.assertEqual([m.imap_uid for m in new], [1])            # «Всі папки» — добирає старіші
        self.assertEqual(EmailMessage.objects.filter(imap_folder_name=self.FOLDER).count(), 4)

    def test_missing_folder(self):
        self.assertIsNone(sync_folder(FakeIMAP({}), self.account, 'Nope'))

    def test_pull_read_flags(self):
        server = FakeIMAP({self.FOLDER: {1: False, 2: False}})
        sync_folder(server, self.account, self.FOLDER)
        m1 = EmailMessage.objects.get(imap_uid=1)
        m2 = EmailMessage.objects.get(imap_uid=2)

        server.folders[self.FOLDER][1] = True              # прочитав на телефоні
        EmailMessage.objects.filter(pk=m2.pk).update(is_read=True)   # прочитав у Minerva, сервер не знає

        pull_read_flags(server, self.account, self.FOLDER, two_way=False)
        m1.refresh_from_db(); m2.refresh_from_db()
        self.assertTrue(m1.is_read)
        self.assertTrue(m2.is_read)                        # one-way: локальне «прочитано» не чіпаємо

        pull_read_flags(server, self.account, self.FOLDER, two_way=True)
        m2.refresh_from_db()
        self.assertFalse(m2.is_read)                       # two-way: сервер — джерело правди
        self.assertTrue(m2.thread.messages.filter(is_read=False).exists())
        m2.thread.refresh_from_db()
        self.assertTrue(m2.thread.has_unread)


@patch('email_assistant.sync._start_background', lambda fn: fn())
class SetReadTests(SyncTestBase):
    def _saved(self, server):
        sync_folder(server, self.account, self.FOLDER)
        return list(EmailMessage.objects.filter(imap_folder_name=self.FOLDER).order_by('imap_uid'))

    def test_pushes_seen_to_the_message_folder(self):
        self._push_read_on()
        server = FakeIMAP({self.FOLDER: {5: False, 6: False}})
        msgs = self._saved(server)
        with patch('email_assistant.imap_client.IMAPClient', server):
            self.assertEqual(set_read(self.account, msgs, True), 2)
        self.assertEqual(server.stored, [(self.FOLDER, [5, 6], True)])
        self.assertFalse(msgs[0].thread.messages.filter(is_read=False).exists())

    def test_no_push_when_setting_off(self):
        es = EmailSettings.get_for_user(self.user)
        es.mark_read_on_server = False
        es.save()
        server = FakeIMAP({self.FOLDER: {7: False}})
        msgs = self._saved(server)
        with patch('email_assistant.imap_client.IMAPClient', server):
            set_read(self.account, msgs, True)
        self.assertEqual(server.stored, [])
        self.assertTrue(EmailMessage.objects.get(imap_uid=7).is_read)


@patch('email_assistant.sync._start_background', lambda fn: fn())
class MailViewsTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def test_preview_marks_read_on_server(self):
        self._push_read_on()
        server = FakeIMAP({'INBOX': {11: False}})
        with patch('email_assistant.imap_client.IMAPClient', server):
            call_command('sync_email', stdout=StringIO())
            em = EmailMessage.objects.get(imap_uid=11)
            r = self.client.get(f'/email/message/{em.pk}/preview/')
        self.assertEqual(r.status_code, 200)
        em.refresh_from_db()
        self.assertTrue(em.is_read)
        self.assertEqual(server.stored, [('INBOX', [11], True)])

    def test_settings_toggle(self):
        r = self.client.post('/email/settings/toggle/', data='{"field": "mark_read_on_server", "value": false}',
                             content_type='application/json')
        self.assertEqual(r.json(), {'ok': True, 'mark_read_on_server': False})
        self.assertFalse(EmailSettings.get_for_user(self.user).mark_read_on_server)
        r = self.client.post('/email/settings/toggle/', data='{"field": "auto_reply_enabled", "value": true}',
                             content_type='application/json')
        self.assertFalse(r.json()['ok'])


class SyncCommandTests(SyncTestBase):
    def test_folders_and_flags(self):
        server = FakeIMAP({'INBOX': {1: False, 2: False}, 'Gesendete Objekte': {}, self.FOLDER: {3: False}})
        with patch('email_assistant.imap_client.IMAPClient', server):
            call_command('sync_email', stdout=StringIO())
            self.assertFalse(EmailMessage.objects.filter(imap_folder_name=self.FOLDER).exists())

            server.folders['INBOX'][1] = True               # прочитано на сервері
            server.folders[self.FOLDER][4] = False
            call_command('sync_email', '--folders', stdout=StringIO())

        self.assertTrue(EmailMessage.objects.get(imap_uid=1, imap_folder_name='INBOX').is_read)
        self.assertFalse(EmailMessage.objects.get(imap_uid=2, imap_folder_name='INBOX').is_read)
        self.assertEqual(
            sorted(EmailMessage.objects.filter(imap_folder_name=self.FOLDER).values_list('imap_uid', flat=True)),
            [3, 4])


# ── Категорії листів, фільтр у скриньці, дії в рядку ─────────────────────────

class CategoryBase(SyncTestBase):
    def setUp(self):
        super().setUp()
        from inventory.models import Supplier
        self.client_c = Customer.objects.create(name='Client Co', email='buyer@client.com')
        Supplier.objects.create(name='Polecoz', email='sales@polecoz.com')

    def _em(self, **kw):
        self._uid = getattr(self, '_uid', 1000) + 1
        data = dict(account=self.account, folder='inbox', imap_folder_name='INBOX', imap_uid=self._uid,
                    to_emails=['viacheslav.pryimak@sevskiy.de'], cc_emails=[], body_text='Hi',
                    sent_at=datetime(2026, 10, 2, tzinfo=dt_tz.utc))
        data.update(kw)
        return EmailMessage.objects.create(**data)


class CategoryTests(CategoryBase):
    def test_categories(self):
        cases = {
            'customer':   dict(from_email='buyer@client.com', subject='PO'),
            'supplier':   dict(from_email='li@polecoz.com', subject='new po'),    # домен постачальника
            'service':    dict(from_email='pkginfo@ups.com', subject='Delivered'),
            'newsletter': dict(from_email='news@shop.example', subject='Deals',
                               body_text='Big sale… To stop receiving, unsubscribe here'),
            'internal':   dict(from_email='sergey@sevskiy.de', subject='Lunch'),
            'other':      dict(from_email='someone@unknown.example', subject='Question'),
        }
        for expected, kw in cases.items():
            em = self._em(**kw)
            em.refresh_from_db()
            self.assertEqual(em.category, expected, kw)

        sent = self._em(folder='sent', imap_folder_name='Gesendete Objekte',
                        from_email='viacheslav.pryimak@sevskiy.de', to_emails=['buyer@client.com'],
                        subject='Offer')
        sent.refresh_from_db()
        self.assertEqual(sent.category, 'customer')       # наш лист клієнту — теж «клієнти»

    def test_noreply_service_and_list_unsubscribe_hint(self):
        em = self._em(from_email='noreply@random-portal.example', subject='Your code')
        em.refresh_from_db()
        self.assertEqual(em.category, 'service')
        server = FakeIMAP({'INBOX': {21: False}})
        with patch('email_assistant.imap_client.IMAPClient', server), \
                patch.object(FakeIMAP, 'fetch_messages',
                             lambda self, folder, **kw: [dict(_msg(21), list_unsubscribe=True)]
                             if folder == 'INBOX' else []):
            call_command('sync_email', stdout=StringIO())
        self.assertEqual(EmailMessage.objects.get(imap_uid=21).category, 'newsletter')

    def test_categorize_pending_fills_history(self):
        em = self._em(from_email='buyer@client.com', subject='Old')
        EmailMessage.objects.filter(pk=em.pk).update(category='')
        out = StringIO()
        call_command('categorize_emails', stdout=out)
        em.refresh_from_db()
        self.assertEqual(em.category, 'customer')
        self.assertIn('👤 Клієнти: 1', out.getvalue())


@patch('email_assistant.sync._start_background', lambda fn: fn())
class InboxFilterTests(CategoryBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        self.customer_mail = self._em(from_email='buyer@client.com', subject='PO 1')
        self.ups_mail = self._em(from_email='pkginfo@ups.com', subject='Delivered 1')
        # гілку прив'язано до клієнта, хоча пише інша людина з компанії
        thread = EmailThread.objects.create(account=self.account, subject='PO', crm_customer=self.client_c)
        self.colleague = self._em(from_email='boss@client-group.com', subject='Escalation', thread=thread)

    def _subjects(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return {m.subject for m in r.context['emails']}, r

    def test_filter_by_category_and_customer(self):
        subjects, r = self._subjects('/email/?folder=inbox&cat=service')
        self.assertEqual(subjects, {'Delivered 1'})
        counts = {key: n for key, _, n, _ in r.context['categories']}
        self.assertEqual(counts['service'], 1)
        self.assertGreaterEqual(counts['customer'], 1)

        subjects, r = self._subjects(f'/email/?folder=inbox&customer={self.client_c.pk}')
        self.assertEqual(subjects, {'PO 1', 'Escalation'})
        self.assertIn(f'customer={self.client_c.pk}', r.context['filter_qs'])
        self.assertContains(r, 'em-row-acts')              # кнопки при наведенні

    def test_delete_moves_from_the_messages_own_folder(self):
        em = self._em(from_email='a@b.example', subject='In custom folder',
                      imap_folder_name='Meine Order', imap_uid=5)
        calls = []
        with patch('email_assistant.views._imap_bg', lambda *a: calls.append(a)):
            r = self.client.post(f'/email/message/{em.pk}/delete/')
        self.assertTrue(r.json()['ok'])
        self.assertEqual(calls, [(self.account.pk, 'Meine Order', 5, 'trash')])


# ── Групування по гілці (⎘) і дії над усією гілкою ───────────────────────────

class FakeIMAPMoves(FakeIMAP):
    def __init__(self, folders):
        super().__init__(folders)
        self.moves = []

    def move_to_trash(self, folder, uid):
        self.moves.append(('trash', folder, uid))
        return True

    def move_to_spam(self, folder, uid):
        self.moves.append(('spam', folder, uid))
        return True


@patch('email_assistant.sync._start_background', lambda fn: fn())
class ThreadGroupingTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        self.thread = EmailThread.objects.create(account=self.account, subject='Long thread')
        # 35 листів у Вхідних + 1 наша відповідь у Надісланих — більше ніж одна сторінка (30)
        for i in range(1, 36):
            self._em(i, 'INBOX', 'inbox', read=(i != 1), thread=self.thread)
        self._em(100, 'Gesendete Objekte', 'sent', read=True, thread=self.thread)
        other = EmailThread.objects.create(account=self.account, subject='Other')
        self.other = self._em(200, 'INBOX', 'inbox', read=False, thread=other)

    def _em(self, uid, imap_folder, folder, read, thread):
        return EmailMessage.objects.create(
            account=self.account, thread=thread, imap_uid=uid, imap_folder_name=imap_folder,
            folder=folder, subject=f'{thread.subject} {uid}', from_email='partner@example.org',
            to_emails=['viacheslav.pryimak@sevskiy.de'], is_read=read,
            sent_at=datetime(2026, 10, 1, tzinfo=dt_tz.utc) + timedelta(minutes=uid))

    def test_grouped_list_counts_all_messages_of_thread(self):
        r = self.client.get('/email/?folder=inbox')
        self.assertEqual(r.context['total'], 36)                 # без групування — кожен лист

        self.client.cookies['em_group'] = '1'
        r = self.client.get('/email/?folder=inbox')
        self.assertTrue(r.context['grouped'])
        self.assertEqual(r.context['total'], 2)                  # дві гілки — два рядки
        rows = {m.thread_id: m for m in r.context['emails']}
        long_row = rows[self.thread.pk]
        self.assertEqual(long_row.imap_uid, 35)                  # найновіший лист гілки у Вхідних
        self.assertEqual(long_row.thread_total, 36)              # усі листи гілки, і Надіслані теж
        self.assertEqual(long_row.thread_unread, 1)              # непрочитаний найстаріший → рядок «непрочитаний»
        self.assertEqual(len(long_row.thread_pks.split(',')), 36)
        self.assertContains(r, 'class="em-thread-count"')
        self.assertContains(r, f'id="row-{long_row.pk}"')
        self.assertFalse(hasattr(rows[self.other.thread_id], 'thread_total') and
                         rows[self.other.thread_id].thread_total > 1)

    def test_bulk_thread_delete_and_read(self):
        self._push_read_on()
        pks = list(self.thread.messages.values_list('pk', flat=True))
        server = FakeIMAPMoves({'INBOX': {}, 'Gesendete Objekte': {}})
        with patch('email_assistant.imap_client.IMAPClient', server):
            r = self.client.post('/email/messages/bulk/', data=json.dumps({'action': 'read', 'pks': pks}),
                                 content_type='application/json')
            self.assertTrue(r.json()['ok'])
            self.assertFalse(self.thread.messages.filter(is_read=False).exists())
            self.assertEqual(server.stored, [('INBOX', [1], True)])   # \Seen — лише тому, що був непрочитаний

            r = self.client.post('/email/messages/bulk/', data=json.dumps({'action': 'delete', 'pks': pks}),
                                 content_type='application/json')
        self.assertTrue(r.json()['ok'])
        self.assertEqual(self.thread.messages.filter(folder='trash', is_deleted=False).count(), 36)
        self.assertIn(('trash', 'Gesendete Objekte', 100), server.moves)   # кожен лист — зі своєї папки
        self.assertEqual(len(server.moves), 36)


# ── Категорія вручну (правий клік) запам'ятовується; мітки в рядках ───────────

@patch('email_assistant.sync._start_background', lambda fn: fn())
class CategoryRuleTests(CategoryBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def _set(self, em, category, scope='sender'):
        return self.client.post(f'/email/message/{em.pk}/category/',
                                data=json.dumps({'category': category, 'scope': scope}),
                                content_type='application/json').json()

    def _cat(self, em):
        em.refresh_from_db()
        return em.category

    def test_sender_rule_is_remembered_and_applied(self):
        first = self._em(from_email='pkginfo@ups.com', subject='Delivered')
        older = self._em(from_email='pkginfo@ups.com', subject='Older one')
        mine = self._em(folder='sent', imap_folder_name='Gesendete Objekte', subject='Question to UPS',
                        from_email='viacheslav.pryimak@sevskiy.de', to_emails=['pkginfo@ups.com'])
        other_ups = self._em(from_email='billing@ups.com', subject='Invoice')
        self.assertEqual(self._cat(first), 'service')

        d = self._set(first, 'supplier')
        self.assertEqual((d['ok'], d['key'], d['category']), (True, 'pkginfo@ups.com', 'supplier'))
        self.assertEqual(self._cat(older), 'supplier')        # усі листи цього контакту
        self.assertEqual(self._cat(mine), 'supplier')         # і мої листи йому
        self.assertEqual(self._cat(other_ups), 'service')     # інша адреса домену — без змін

        new = self._em(from_email='pkginfo@ups.com', subject='Tomorrow')
        self.assertEqual(self._cat(new), 'supplier')          # нові листи — за правилом

        self._set(first, 'newsletter', scope='domain')
        self.assertEqual(self._cat(other_ups), 'newsletter')  # правило на весь @ups.com
        self.assertEqual(self._cat(first), 'supplier')        # точна адреса важливіша за домен

        self._set(first, '')                                  # ↺ Автоматично — забути обидва
        self.assertFalse(EmailCategoryRule.objects.exists())
        self.assertEqual(self._cat(first), 'service')
        self.assertEqual(self._cat(other_ups), 'service')

    def test_internal_mail_has_no_contact(self):
        em = self._em(from_email='sergey@sevskiy.de', subject='Lunch')
        d = self._set(em, 'customer')
        self.assertFalse(d['ok'])

    def test_rows_show_customer_and_category_tags(self):
        self._em(from_email='buyer@client.com', subject='PO 7')
        self._em(from_email='pkginfo@ups.com', subject='Delivered 7')
        r = self.client.get('/email/?folder=inbox')
        self.assertContains(r, '👤 Client Co')
        self.assertContains(r, 'class="em-tag cat-service"')
        self.assertContains(r, 'data-contact="pkginfo@ups.com"')
        self.assertContains(r, "ctxSetCategory('supplier')")       # підменю правого кліку


# ── Зв'язки листа (замовлення / трек-номер), пошук по всіх папках, копіювання адрес ──

UPS_TEXT = (
    'Von: deern1@ups.com\nBetreff: **Reminder** UPS [E-BGF489] Tracking Number: 1Z0R6B300426098447\n'
    'REF: 102037400 SHIPPER : 0R6B30 SEVSKIY GMBH\nREF: CONSIGNEE : AEVEX 8514 SUNSTATE ST TAMPA'
)


@patch('email_assistant.sync._start_background', lambda fn: fn())
class MailLinksTests(CategoryBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        from shipping.models import Carrier, Shipment
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        self.aevex = Customer.objects.create(name='AEVEX', email='ifejzic@aevex.com')
        self.order = SalesOrder.objects.create(
            source='digikey', order_number='102037400', customer_key=self.aevex.external_key,
            email=self.aevex.email, affects_stock=False)
        carrier = Carrier.objects.create(name='UPS', carrier_type='ups')
        self.shipment = Shipment.objects.create(order=self.order, carrier=carrier, status='in_transit',
                                                tracking_number='1Z0R6B300426098447')

    def test_find_links_by_order_and_tracking(self):
        from email_assistant.links import find_links
        links = find_links(['Exception', UPS_TEXT])
        self.assertEqual(len(links), 1)
        link = links[0]
        self.assertEqual((link['label'], link['customer'], link['reason']),
                         ('digikey:102037400', self.aevex, 'номер замовлення'))
        self.assertEqual(link['shipments'][0]['tracking'], '1Z0R6B300426098447')
        self.assertEqual(link['shipments'][0]['status'], 'В дорозі')

        only_tracking = find_links(['Your parcel 1z0r6b300426098447 is delayed'])   # регістр не важливий
        self.assertEqual((only_tracking[0]['order'], only_tracking[0]['reason']), (self.order, 'трек-номер'))
        self.assertEqual(find_links(['Hello, nothing here 2026']), [])

    def test_forwarded_ups_mail_shows_links_in_preview(self):
        fwd = self._em(from_email='sergey@sevskiy.de', to_emails=['viacheslav.pryimak@sevskiy.de'],
                       subject='WG: **Reminder** UPS [E-BGF489] Exception Notification', body_text=UPS_TEXT)
        r = self.client.get(f'/email/message/{fwd.pk}/preview/')
        self.assertContains(r, '🛒 digikey:102037400')
        self.assertContains(r, '👤 AEVEX')
        self.assertContains(r, '🚚 1Z0R6B300426098447 · В дорозі')
        self.assertContains(r, 'class="em-addr" data-addr="viacheslav.pryimak@sevskiy.de"')   # адресу можна скопіювати

    def test_carrier_mail_with_tracking_only_goes_to_customer_timeline(self):
        self._em(from_email='deern1@ups.com', subject='UPS Exception',
                 body_text='Tracking Number: 1Z0R6B300426098447 — import fees unpaid')
        ev = CustomerTimeline.objects.get(customer=self.aevex, title='UPS Exception')
        self.assertEqual(ev.related_order_id, self.order.pk)


@patch('email_assistant.sync._start_background', lambda fn: fn())
class SearchAllFoldersTests(CategoryBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        self._em(from_email='li@polecoz.com', subject='Polecoz PO in inbox')
        self._em(folder='sent', imap_folder_name='Gesendete Objekte', from_email='viacheslav.pryimak@sevskiy.de',
                 to_emails=['li@polecoz.com'], subject='Polecoz reply sent')
        self._em(imap_folder_name='Meine Order', from_email='li@polecoz.com', subject='Polecoz in custom folder')
        self._em(folder='spam', is_spam=True, from_email='x@spam.example', subject='Polecoz spam')
        sent_thread = EmailThread.objects.create(account=self.account, subject='Offer', crm_customer=self.client_c)
        self._em(folder='sent', imap_folder_name='Gesendete Objekte', thread=sent_thread,
                 from_email='viacheslav.pryimak@sevskiy.de', to_emails=['buyer@client.com'], subject='Offer')

    def test_search_covers_all_folders_except_spam(self):
        r = self.client.get('/email/?folder=inbox&q=Polecoz')
        self.assertTrue(r.context['search_all'])
        self.assertEqual({m.subject for m in r.context['emails']},
                         {'Polecoz PO in inbox', 'Polecoz reply sent', 'Polecoz in custom folder'})
        tags = {m.subject: m.folder_tag for m in r.context['emails']}
        self.assertEqual(tags['Polecoz reply sent'], '📤 Надіслані')
        self.assertEqual(tags['Polecoz in custom folder'], '📁 Meine Order')
        self.assertContains(r, '🔎 Усі папки')

        r = self.client.get('/email/?folder=inbox')                 # без пошуку — лише папка
        self.assertFalse(r.context['search_all'])
        self.assertNotIn('Polecoz reply sent', {m.subject for m in r.context['emails']})

    def test_customer_filter_and_list_cover_all_folders(self):
        r = self.client.get('/email/?folder=inbox')
        names = {c['name'] for c in r.context['mail_customers']}
        self.assertIn('Client Co', names)                           # клієнт лише з «Надісланих»
        self.assertContains(r, 'id="em-customers-data"')

        r = self.client.get(f'/email/?folder=inbox&customer={self.client_c.pk}')
        self.assertEqual({m.subject for m in r.context['emails']}, {'Offer'})


# ── Досьє листа: AI-відповідь з фактами Minerva та «📜 Історія» ─────────────────

class MailContextTests(MailLinksTests):
    def setUp(self):
        super().setUp()
        from datetime import date
        SalesOrder.objects.filter(pk=self.order.pk).update(order_date=date(2026, 10, 6),
                                                          shipped_at=date(2026, 10, 6))
        self.thread = EmailThread.objects.create(account=self.account, subject='UPS exception')
        self.fwd = self._em(thread=self.thread, from_email='sergey@sevskiy.de',
                            to_emails=['viacheslav.pryimak@sevskiy.de'],
                            subject='WG: **Reminder** UPS [E-BGF489] Exception Notification', body_text=UPS_TEXT)
        old = EmailThread.objects.create(account=self.account, subject='Your order #102037400 has been shipped',
                                         crm_customer=self.aevex)
        self._em(thread=old, folder='sent', imap_folder_name='Gesendete Objekte',
                 from_email='viacheslav.pryimak@sevskiy.de', to_emails=['ifejzic@aevex.com'],
                 subject='Your order #102037400 has been shipped', body_text='Dear IDA, your order has been shipped.')
        CustomerTimeline.objects.create(customer=self.aevex, event_type='call', title='Travis promised to pay fees')

    def test_context_collects_order_shipment_customer_and_history(self):
        from email_assistant.context import build_context, context_text, history_events, state_lines
        ctx = build_context(self.fwd)
        self.assertEqual(ctx['customer'], self.aevex)
        self.assertEqual(ctx['orders'], [self.order])
        state = '\n'.join(state_lines(ctx))
        self.assertIn('🛒 digikey:102037400 від 06.10.2026', state)
        self.assertIn('🚚 UPS 1Z0R6B300426098447 — В дорозі', state)
        events = history_events(ctx)
        texts = [t for _, _, t, _ in events]
        scopes = {t: scope for _, _, t, scope in events}
        self.assertEqual(scopes['Замовлення digikey:102037400 відправлено'], 'order')
        # інша переписка про ЦЕ замовлення — основне; дзвінок без номера — «ще по клієнту»
        self.assertEqual(scopes['Інша переписка: «Your order #102037400 has been shipped» (1 лист.)'], 'order')
        self.assertEqual(scopes['📞 Дзвінок: Travis promised to pay fees'], 'customer')
        self.assertIn('Замовлення digikey:102037400 відправлено', texts)
        self.assertTrue(any(t.startswith('Інша переписка: «Your order #102037400') for t in texts))
        self.assertTrue(any('Travis promised to pay fees' in t for t in texts))
        facts = context_text(ctx)
        self.assertIn('ІНШІ ЛИСТУВАННЯ З ЦИМ КЛІЄНТОМ', facts)
        self.assertIn('Dear IDA, your order has been shipped.', facts)

    def test_ai_reply_gets_instruction_and_facts(self):
        prompts = []
        with patch('email_assistant.ai_helper._call_ai_direct',
                   lambda prompt, max_tokens=1024, user=None: prompts.append(prompt) or 'Dear Travis, …'):
            r = self.client.get(f'/email/message/{self.fwd.pk}/ai-reply/',
                                {'prompt': 'Попроси клієнта оплатити мито до 27.10'})
        d = r.json()
        self.assertTrue(d['ok'])
        self.assertIn('замовлення digikey:102037400', d['used'])
        prompt = prompts[0]
        self.assertIn('ЗАВДАННЯ: Попроси клієнта оплатити мито до 27.10', prompt)   # інструкція більше не губиться
        self.assertIn('ФАКТИ З MINERVA', prompt)
        self.assertIn('1Z0R6B300426098447', prompt)
        self.assertIn('Нічого не вигадуй', prompt)

    def test_history_endpoint(self):
        d = self.client.get(f'/email/message/{self.fwd.pk}/history/').json()
        self.assertTrue(any('digikey:102037400' in s for s in d['state']))
        self.assertTrue(any(e['icon'] == '📦' for e in d['events']))
        self.assertNotIn('summary', d)                                    # без ?ai=1 — лише факти, без AI

        with patch('email_assistant.ai_helper._call_ai_direct', lambda prompt, max_tokens=1024, user=None: '1) Мито 35 USD…'):
            d = self.client.get(f'/email/message/{self.fwd.pk}/history/', {'ai': '1'}).json()
        self.assertEqual(d['summary'], '1) Мито 35 USD…')

        r = self.client.get(f'/email/message/{self.fwd.pk}/preview/')
        self.assertContains(r, '📜 Історія')


# ── Вибір AI-моделі пошти, параметри запиту, порядок листів у гілці ─────────────

class _FakeBadRequest(Exception):
    pass


class _FakeAnthropicModule:
    """Підміна пакета anthropic: записує параметри messages.create і віддає задану відповідь."""
    BadRequestError = _FakeBadRequest

    def __init__(self, response=None, reject_fallbacks=False):
        self.calls = []
        self.response = response
        self.reject_fallbacks = reject_fallbacks
        module = self

        class _Messages:
            def create(self, **kw):
                module.calls.append(kw)
                if module.reject_fallbacks and 'extra_body' in kw:
                    raise _FakeBadRequest('fallbacks not allowed')
                return module.response

        class _Client:
            def __init__(self, api_key=None):
                self.messages = _Messages()

        self.Anthropic = _Client


def _response(text='Draft', stop_reason='end_turn', model='claude-opus-5-5'):
    from types import SimpleNamespace as NS
    return NS(model=model, stop_reason=stop_reason, stop_details=None,
              content=[NS(type='thinking', thinking=''), NS(type='text', text=text)])


class AIModelTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        from strategy.models import AISettings
        s = AISettings.get()
        s.anthropic_api_key = 'test-key'
        s.save()

    def _set_model(self, model):
        es = EmailSettings.get_for_user(self.user)
        es.ai_model = model
        es.save()

    def test_resolve_model(self):
        from email_assistant.ai_helper import resolve_model
        self.assertEqual(resolve_model('Привіт', self.user), 'claude-haiku-4-5-20251001')   # авто = роутер
        self.assertEqual(resolve_model('Зроби аналіз', self.user), 'claude-sonnet-4-6')
        self._set_model('claude-opus-5-5')
        self.assertEqual(resolve_model('Привіт', self.user), 'claude-opus-5-5')
        self.assertEqual(resolve_model('Привіт', None), 'claude-haiku-4-5-20251001')        # фонові задачі

    def test_new_model_request_params_and_fallbacks(self):
        import sys
        from email_assistant import ai_helper
        self._set_model('claude-opus-5-5')
        fake = _FakeAnthropicModule(_response('Dear Travis'))
        with patch.dict(sys.modules, {'anthropic': fake}):
            text = ai_helper._call_ai_direct('Напиши', max_tokens=700, user=self.user)
        self.assertEqual(text, 'Dear Travis')                         # лише текст, без блоків роздумів
        call = fake.calls[0]
        self.assertEqual(call['model'], 'claude-opus-5-5')
        self.assertEqual(call['max_tokens'], 16000)                   # роздуми не з'їдають відповідь
        self.assertEqual(call['extra_body'], {'fallbacks': 'default'})
        self.assertEqual(call['extra_headers'], {'anthropic-beta': 'server-side-fallback-2026-07-01'})
        self.assertEqual(ai_helper.last_model(), 'claude-opus-5-5')

    def test_fallbacks_rejected_retry_and_refusal(self):
        import sys
        from email_assistant import ai_helper
        self._set_model('claude-sonnet-5-5')
        fake = _FakeAnthropicModule(_response('OK', model='claude-sonnet-5-5'), reject_fallbacks=True)
        with patch.dict(sys.modules, {'anthropic': fake}):
            self.assertEqual(ai_helper._call_ai_direct('x', user=self.user), 'OK')
        self.assertEqual(len(fake.calls), 2)
        self.assertNotIn('extra_body', fake.calls[1])

        self._set_model('claude-haiku-5-5')                           # Haiku: без fallbacks
        fake = _FakeAnthropicModule(_response('', stop_reason='refusal', model='claude-haiku-5-5'))
        with patch.dict(sys.modules, {'anthropic': fake}):
            self.assertEqual(ai_helper._call_ai_direct('x', user=self.user), '')
        self.assertNotIn('extra_body', fake.calls[0])

    def test_auto_keeps_old_behaviour(self):
        import sys
        from email_assistant import ai_helper
        fake = _FakeAnthropicModule(_response('Hi', model='claude-haiku-4-5-20251001'))
        with patch.dict(sys.modules, {'anthropic': fake}):
            ai_helper._call_ai_direct('Привіт', max_tokens=1024, user=self.user)
        self.assertEqual(fake.calls[0]['model'], 'claude-haiku-4-5-20251001')
        self.assertEqual(fake.calls[0]['max_tokens'], 1024)
        self.assertNotIn('extra_body', fake.calls[0])


@patch('email_assistant.sync._start_background', lambda fn: fn())
class MailSettingsAndOrderTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def test_ai_model_setting(self):
        post = lambda value: self.client.post(
            '/email/settings/toggle/', data=json.dumps({'field': 'ai_model', 'value': value}),
            content_type='application/json').json()
        self.assertEqual(post('claude-sonnet-5-5'), {'ok': True, 'ai_model': 'claude-sonnet-5-5'})
        self.assertEqual(EmailSettings.get_for_user(self.user).ai_model, 'claude-sonnet-5-5')
        self.assertFalse(post('gpt-4')['ok'])
        r = self.client.get('/email/')
        self.assertContains(r, 'id="em-ai-model"')
        self.assertContains(r, '<option value="claude-sonnet-5-5" selected>')

    def test_thread_newest_first_with_toggle(self):
        thread = EmailThread.objects.create(account=self.account, subject='Neue Testboards')
        for i, day in enumerate([25, 1, 7], 1):
            EmailMessage.objects.create(
                account=self.account, thread=thread, imap_uid=i, imap_folder_name='INBOX', folder='inbox',
                subject=f'Neue Testboards {i}', from_email='martin@iot.example', is_read=True,
                sent_at=datetime(2026, 9 if day == 25 else 10, day, 12, tzinfo=dt_tz.utc))
        r = self.client.get(f'/email/thread/{thread.pk}/preview/')
        subjects = [m.subject for m in r.context['emails']]
        self.assertEqual(subjects, ['Neue Testboards 3', 'Neue Testboards 2', 'Neue Testboards 1'])  # нові зверху
        self.assertContains(r, '⇅ Нові зверху')
        self.assertContains(r, 'Neue Testboards 1</div>')            # заголовок — тема першого листа
        newest = EmailMessage.objects.get(imap_uid=3)
        self.assertContains(r, f'class="em-card expanded" id="pv-card-{newest.pk}"')

        self.client.cookies['em_thread_order'] = 'old'
        r = self.client.get(f'/email/thread/{thread.pk}/preview/')
        self.assertEqual([m.subject for m in r.context['emails']][0], 'Neue Testboards 1')
        self.assertContains(r, '⇅ Старі зверху')
        self.assertContains(r, f'class="em-card expanded" id="pv-card-{newest.pk}"')  # розгорнутий — найновіший


class ReplyRecipientTests(MailContextTests):
    def test_forwarded_mail_reply_goes_to_customer(self):
        long_tail = UPS_TEXT + '\n' + ('x' * 3000) + '\nAntwort erforderlich bis 10/27/2026'
        EmailMessage.objects.filter(pk=self.fwd.pk).update(body_text=long_tail)
        type(self.shipment).objects.filter(pk=self.shipment.pk).update(
            carrier_status_label="2026-10-14 06:25 — We'll contact the sender or receiver")
        prompts = []
        with patch('email_assistant.ai_helper._call_ai_direct',
                   lambda prompt, max_tokens=1024, user=None: prompts.append(prompt) or 'Dear Ida, …'):
            d = self.client.get(f'/email/message/{self.fwd.pk}/ai-reply/').json()
            s = self.client.get(f'/email/message/{self.fwd.pk}/history/', {'ai': '1'}).json()
        self.assertEqual(d['to'], 'ifejzic@aevex.com')                  # не колега, що переслав, а клієнт
        self.assertIn('АДРЕСАТ ЛИСТА: AEVEX <ifejzic@aevex.com>', prompts[0])
        self.assertIn('Antwort erforderlich bis 10/27/2026', prompts[0])  # найновіший лист — не обрізаний
        self.assertIn('без Markdown', prompts[1])
        self.assertIn('остання подія перевізника', '\n'.join(s['state']))  # час події ≠ термін

    def test_reply_to_customer_mail_goes_to_sender(self):
        incoming = self._em(thread=self.thread, from_email='ifejzic@aevex.com', from_name='Ida Fejzic',
                            subject='RE: UPS fee', body_text='We paid the fee.')
        with patch('email_assistant.ai_helper._call_ai_direct',
                   lambda prompt, max_tokens=1024, user=None: 'Thank you'):
            d = self.client.get(f'/email/message/{incoming.pk}/ai-reply/').json()
        self.assertEqual(d['to'], 'ifejzic@aevex.com')


class DraftAndTranslateTests(SyncTestBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def test_strip_signoff(self):
        from email_assistant.ai_helper import strip_signoff
        self.assertEqual(strip_signoff('Dear Travis,\n\nPlease pay.\n\nBest regards,\nViacheslav'),
                         'Dear Travis,\n\nPlease pay.')
        self.assertEqual(strip_signoff('Hallo,\nbitte zahlen.\n\nMit freundlichen Grüßen\nViacheslav Pryimak'),
                         'Hallo,\nbitte zahlen.')
        self.assertEqual(strip_signoff('Thank you for your assistance.\nPlease confirm.'),
                         'Thank you for your assistance.\nPlease confirm.')

    def test_translate_to_chosen_language(self):
        prompts = []
        with patch('email_assistant.ai_helper._call_ai_direct',
                   lambda prompt, max_tokens=1024, user=None: prompts.append(prompt) or 'Sehr geehrter Herr'):
            r = self.client.post('/email/translate-text/', data=json.dumps({'text': 'Шановний пане', 'lang': 'de'}),
                                 content_type='application/json').json()
        self.assertEqual((r['ok'], r['lang']), (True, 'de'))
        self.assertIn('німецьку', prompts[0])

    def test_model_label(self):
        from email_assistant.views import _model_label
        self.assertEqual(_model_label('claude-opus-5-5'), 'Claude Opus 5.5')
        self.assertEqual(_model_label('claude-haiku-4-5-20251001'), 'Claude Haiku 4.5')


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.calls = host, port, []
        _FakeSMTP.instances.append(self)

    def starttls(self, **kw):
        self.calls.append('starttls')

    def login(self, user, password):
        self.calls.append('login')

    def sendmail(self, frm, to, data):
        self.calls.append(('sendmail', tuple(to)))

    def quit(self):
        self.calls.append('quit')


class _FakeImapAppend:
    def __call__(self, account):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def append_to_sent(self, data):
        return True


class SMTPSSLMismatchTests(SyncTestBase):
    def _wrong_version(self, *a, **kw):
        import ssl
        raise ssl.SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1016)')

    def test_ssl_on_starttls_port_falls_back_to_starttls(self):
        from email_assistant.smtp_client import SMTPClient
        self.account.smtp_port, self.account.smtp_use_ssl, self.account.smtp_use_tls = 587, True, False
        _FakeSMTP.instances = []
        with patch('email_assistant.smtp_client.smtplib.SMTP_SSL', self._wrong_version), \
                patch('email_assistant.smtp_client.smtplib.SMTP', _FakeSMTP), \
                patch('email_assistant.imap_client.IMAPClient', _FakeImapAppend()):
            r = SMTPClient(self.account).send(['tthreatts@aevex.com'], 'Re: order', 'Dear Travis',
                                              cc_emails=['sergey@sevskiy.de'])
        self.assertTrue(r['ok'], r)
        calls = _FakeSMTP.instances[0].calls
        self.assertEqual(calls[0], 'starttls')
        self.assertIn(('sendmail', ('tthreatts@aevex.com', 'sergey@sevskiy.de')), calls)
        self.assertTrue(r['mode_fixed'])
        self.assertTrue(r['via'].startswith('viacheslav.pryimak@sevskiy.de · smtp.ionos.de:587 · STARTTLS'))

    def test_correct_settings_report_route(self):
        from email_assistant.smtp_client import SMTPClient
        self.account.smtp_port, self.account.smtp_use_ssl, self.account.smtp_use_tls = 587, False, True
        with patch('email_assistant.smtp_client.smtplib.SMTP', _FakeSMTP), \
                patch('email_assistant.imap_client.IMAPClient', _FakeImapAppend()):
            r = SMTPClient(self.account).send(['tthreatts@aevex.com'], 'Re: order', 'Dear Travis')
        self.assertEqual((r['ok'], r['mode_fixed']), (True, False))
        self.assertEqual(r['via'], 'viacheslav.pryimak@sevskiy.de · smtp.ionos.de:587 · STARTTLS')

    def test_both_modes_fail_gives_clear_hint(self):
        from email_assistant.smtp_client import SMTPClient
        self.account.smtp_port, self.account.smtp_use_ssl = 587, True
        with patch('email_assistant.smtp_client.smtplib.SMTP_SSL', self._wrong_version), \
                patch('email_assistant.smtp_client.smtplib.SMTP', self._wrong_version):
            r = SMTPClient(self.account).send(['a@b.example'], 'S', 'T')
        self.assertFalse(r['ok'])
        self.assertIn('587 — TLS (STARTTLS) без SSL', r['error'])


class ReplyAttachmentsAndContactsTests(MailLinksTests):
    """Вкладення у відповіді (документи замовлення, файли з листування) і підказки адрес."""

    def setUp(self):
        super().setUp()
        import os
        import tempfile
        from django.test import override_settings
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        folder = os.path.join(self._media, 'orders', 'digikey', '102037400')
        os.makedirs(folder)
        with open(os.path.join(folder, 'Invoice_102037400.pdf'), 'wb') as fh:
            fh.write(b'%PDF-1.4 invoice')
        with open(os.path.join(self._media, 'secret.txt'), 'w') as fh:
            fh.write('do not send')

    def tearDown(self):
        import shutil
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)
        super().tearDown()

    def _send(self, **extra):
        sent = {}

        def fake_send(client, **kw):
            sent.update(kw)
            return {'ok': True, 'message_id': '<x@sevskiy.de>', 'via': 'test'}
        data = {'to': 'ifejzic@aevex.com', 'subject': 'Invoice', 'body': 'Please find attached.'}
        data.update(extra)
        with patch('email_assistant.smtp_client.SMTPClient.send', fake_send):
            r = self.client.post('/email/send/', data)
        return r.json(), sent

    def test_order_document_and_thread_file_are_attached(self):
        import os
        att_dir = os.path.join(self._media, 'email_attachments')
        os.makedirs(att_dir)
        with open(os.path.join(att_dir, 'cn23.pdf'), 'wb') as fh:
            fh.write(b'%PDF cn23')
        mail = self._em(subject='CN23', from_email='ifejzic@aevex.com',
                        attachments=[{'name': 'cn23.pdf', 'content_type': 'application/pdf', 'size': 9,
                                      'file_path': 'email_attachments/cn23.pdf'}])
        d, sent = self._send(att_ref=[f'order:{self.order.pk}:Invoice_102037400.pdf', f'msg:{mail.pk}:0'])
        self.assertTrue(d['ok'], d)
        names = {a['name']: a['content'] for a in sent['attachments']}
        self.assertEqual(names['Invoice_102037400.pdf'], b'%PDF-1.4 invoice')
        self.assertEqual(names['cn23.pdf'], b'%PDF cn23')
        stored = EmailMessage.objects.filter(folder='sent', subject='Invoice').first()
        self.assertEqual([a['name'] for a in stored.attachments], ['Invoice_102037400.pdf', 'cn23.pdf'])

    def test_refs_cannot_escape_order_folder(self):
        d, sent = self._send(att_ref=[f'order:{self.order.pk}:../../secret.txt'])
        self.assertFalse(d['ok'])
        self.assertEqual(sent, {})

    def test_preview_lists_order_documents(self):
        fwd = self._em(from_email='sergey@sevskiy.de', subject='WG: UPS', body_text=UPS_TEXT)
        r = self.client.get(f'/email/message/{fwd.pk}/preview/')
        self.assertContains(r, f'data-ref="order:{self.order.pk}:Invoice_102037400.pdf"')
        self.assertContains(r, 'id="pv-reply-to-ac"')

    def test_contact_suggestions(self):
        from email_assistant.models import EmailContact
        EmailContact.objects.create(user=self.user, email='tthreatts@aevex.com', name='Travis Threatts', use_count=3)
        self._em(from_email='martin.dobler@iot-invent.com', from_name='Martin Dobler', subject='Testboards')
        found = lambda q: self.client.get('/email/crm-contacts/', {'q': q}).json()['contacts']
        self.assertEqual(found('mart')[0]['email'], 'martin.dobler@iot-invent.com')
        emails = [c['email'] for c in found('aevex')]
        self.assertIn('tthreatts@aevex.com', emails)                 # адресна книга
        self.assertIn('ifejzic@aevex.com', emails)                   # клієнт CRM
        self.assertEqual(found('Travis')[0]['source'], 'адресна книга')


class ReplyThreadingTests(MailLinksTests):
    """Відповідь лягає в ту саму гілку (In-Reply-To/References), Копія підставляється, «Надіслані» не дублюються."""

    def _data(self, uid, **kw):
        d = _msg(uid)
        d.update(kw)
        return d

    def test_references_chain_joins_thread(self):
        from email_assistant.sync import save_message
        first = save_message(self.account, self._data(1, subject='Anfrage Boards', message_id='<a@x>'), 'INBOX')
        # третій лист посилається на перший лише через References (другого в Minerva нема)
        third = save_message(self.account, self._data(3, subject='AW: AW: Anfrage Boards', message_id='<c@x>',
                                                      in_reply_to='<b@x>', references=['<a@x>', '<b@x>']),
                             'INBOX')
        self.assertEqual(third.thread_id, first.thread_id)
        self.assertEqual(third.thread.message_count, 2)

    def test_subject_fallback_needs_same_counterpart(self):
        from email_assistant.sync import normalize_subject, save_message
        self.assertEqual(normalize_subject('AW: Re[2]: WG:  Anfrage   Boards'), 'anfrage boards')
        a = save_message(self.account, self._data(1, subject='Anfrage', from_email='one@client.com'), 'INBOX')
        reply = save_message(self.account, self._data(2, subject='AW: Anfrage', from_email='one@client.com'), 'INBOX')
        other = save_message(self.account, self._data(3, subject='Anfrage', from_email='two@other.com'), 'INBOX')
        self.assertEqual(reply.thread_id, a.thread_id)          # без заголовків, але та сама людина й тема
        self.assertNotEqual(other.thread_id, a.thread_id)       # однакова тема від іншої людини — окремо

    def test_reply_sends_headers_and_sent_copy_is_not_duplicated(self):
        from email_assistant.sync import save_message
        orig = self._em(from_email='tthreatts@aevex.com', subject='RE: DigiKey order 102037400',
                        to_emails=['viacheslav.pryimak@sevskiy.de', 'sergey@sevskiy.de'],
                        cc_emails=['ifejzic@aevex.com'], message_id='<orig@aevex.com>',
                        in_reply_to='<prev@sevskiy.de>')
        sent = {}

        def fake_send(client, **kw):
            sent.update(kw)
            return {'ok': True, 'message_id': '<reply1@sevskiy.de>', 'via': 'test'}
        with patch('email_assistant.smtp_client.SMTPClient.send', fake_send):
            d = self.client.post('/email/send/', {'reply_to_pk': orig.pk, 'to': 'tthreatts@aevex.com',
                                                  'cc': 'sergey@sevskiy.de, ifejzic@aevex.com',
                                                  'subject': 'RE: DigiKey order 102037400', 'body': 'Hi'}).json()
        self.assertTrue(d['ok'], d)
        self.assertEqual(sent['reply_to_message'], orig)
        orig.refresh_from_db()
        self.assertIsNotNone(orig.thread_id)
        local = EmailMessage.objects.get(message_id='<reply1@sevskiy.de>')
        self.assertEqual((local.thread_id, local.in_reply_to), (orig.thread_id, '<orig@aevex.com>'))

        # копія з «Надісланих» приходить синхронізацією — дописує UID, а не створює другий лист
        copy = self._data(77, message_id='<reply1@sevskiy.de>', subject='RE: DigiKey order 102037400',
                          from_email='viacheslav.pryimak@sevskiy.de', to_emails=['tthreatts@aevex.com'],
                          in_reply_to='<orig@aevex.com>', is_read=True)
        self.assertIsNone(save_message(self.account, copy, 'Gesendete Objekte', 'sent'))
        local.refresh_from_db()
        self.assertEqual((local.imap_uid, local.imap_folder_name), (77, 'Gesendete Objekte'))
        self.assertEqual(EmailMessage.objects.filter(message_id='<reply1@sevskiy.de>').count(), 1)

        # відповідь клієнта на наш лист — у ту саму гілку
        answer = save_message(self.account, self._data(78, message_id='<ans@aevex.com>', subject='RE: RE: x',
                                                       from_email='tthreatts@aevex.com',
                                                       in_reply_to='<reply1@sevskiy.de>'), 'INBOX')
        self.assertEqual(answer.thread_id, orig.thread_id)

    def test_preview_reply_targets_last_message(self):
        """Вбудована відповідь у прев'ю знає, на який лист відповідає (інакше йшла новим листом без гілки)."""
        import re as _re
        msg = self._em(from_email='buyer@client.com', subject='Anfrage Boards', message_id='<p1@client.com>')
        html = self.client.get(f'/email/message/{msg.pk}/preview/').content.decode()
        self.assertRegex(html, rf'_pvReplyPk\s*=\s*{msg.pk};')
        self.assertIn("_pvReplySubj = 'Anfrage Boards'", html)
        self.assertIn(f'data-subject="Anfrage Boards"', html)
        self.assertEqual(len(_re.findall(r'<textarea class="em-ai-prompt-inp"', html)), 2)   # AI-поля — багаторядкові

    def test_reply_without_subject_gets_re_subject(self):
        orig = self._em(from_email='buyer@client.com', subject='Anfrage Boards', message_id='<q1@client.com>')
        aw = self._em(from_email='buyer@client.com', subject='AW: Lieferung', message_id='<q2@client.com>')
        sent = []

        def fake_send(client, **kw):
            sent.append(kw)
            return {'ok': True, 'message_id': f'<r{len(sent)}@sevskiy.de>', 'via': 'test'}
        with patch('email_assistant.smtp_client.SMTPClient.send', fake_send):
            for m in (orig, aw):
                d = self.client.post('/email/send/', {'reply_to_pk': m.pk, 'to': 'buyer@client.com',
                                                      'subject': '  ', 'body': 'Hi'}).json()
                self.assertTrue(d['ok'], d)
            d = self.client.post('/email/send/', {'to': 'buyer@client.com', 'subject': '', 'body': 'Hi'}).json()
        self.assertEqual([s['subject'] for s in sent], ['Re: Anfrage Boards', 'AW: Lieferung'])
        self.assertEqual(sent[0]['reply_to_message'], orig)
        self.assertEqual(EmailMessage.objects.get(message_id='<r1@sevskiy.de>').subject, 'Re: Anfrage Boards')
        self.assertFalse(d['ok'])                      # новий лист без теми — як і раніше, помилка

    def test_two_sends_without_sync_do_not_collide(self):
        n = iter(range(1, 10))

        def fake_send(client, **kw):
            return {'ok': True, 'message_id': f'<new{next(n)}@sevskiy.de>', 'via': 'test'}
        with patch('email_assistant.smtp_client.SMTPClient.send', fake_send):
            for _ in range(2):
                self.assertTrue(self.client.post('/email/send/', {
                    'to': 'buyer@client.com', 'subject': 'Angebot', 'body': 'Hi'}).json()['ok'])
        sent = EmailMessage.objects.filter(folder='sent', subject='Angebot')
        self.assertEqual(sent.count(), 2)
        self.assertTrue(all(m.thread_id for m in sent))          # новий лист теж має гілку

    def test_smtp_references_chain(self):
        from email_assistant.smtp_client import SMTPClient
        orig = self._em(from_email='tthreatts@aevex.com', subject='RE: order', message_id='<orig@aevex.com>',
                        in_reply_to='<prev@sevskiy.de>')
        captured = {}

        class _Capture(_FakeSMTP):
            def sendmail(self, frm, to, data):
                captured['data'] = data
        self.account.smtp_port, self.account.smtp_use_ssl, self.account.smtp_use_tls = 587, False, True
        with patch('email_assistant.smtp_client.smtplib.SMTP', _Capture), \
                patch('email_assistant.imap_client.IMAPClient', _FakeImapAppend()):
            r = SMTPClient(self.account).send(['tthreatts@aevex.com'], 'RE: order', 'Hi', reply_to_message=orig)
        self.assertTrue(r['ok'], r)
        raw = captured['data'] if isinstance(captured['data'], str) else captured['data'].decode()
        self.assertIn('In-Reply-To: <orig@aevex.com>', raw)
        self.assertIn('References: <prev@sevskiy.de> <orig@aevex.com>', raw)

    def test_reply_cc_prefilled(self):
        orig = self._em(from_email='tthreatts@aevex.com', subject='AW: Boards',
                        to_emails=['viacheslav.pryimak@sevskiy.de', 'sergey@sevskiy.de'],
                        cc_emails=['ifejzic@aevex.com', 'Viacheslav.Pryimak@sevskiy.de'])
        r = self.client.get('/email/compose/', {'reply_to': orig.pk})
        self.assertContains(r, f'name="reply_to_pk" value="{orig.pk}"')
        self.assertContains(r, 'value="sergey@sevskiy.de, ifejzic@aevex.com"')
        self.assertContains(r, 'value="AW: Boards"')              # без «Re: AW:»
        p = self.client.get(f'/email/message/{orig.pk}/preview/')
        self.assertContains(p, "pvOpenReply('reply','tthreatts@aevex.com','AW: Boards',"
                               "'sergey@sevskiy.de, ifejzic@aevex.com')")

        # відповідь на власний лист — тим самим отримувачам
        mine = self._em(from_email='viacheslav.pryimak@sevskiy.de', folder='sent', subject='Offer',
                        to_emails=['buyer@client.com', 'boss@client.com'], cc_emails=['sergey@sevskiy.de'])
        from email_assistant.views import _reply_addresses
        self.assertEqual(_reply_addresses(self.account, mine),
                         ('buyer@client.com', ['boss@client.com', 'sergey@sevskiy.de']))


class QuickFilterAllMailTests(CategoryBase):
    """Фільтр над списком шукає по всіх листах, а не лише на поточній сторінці."""

    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)

    def test_old_mail_beyond_first_page_is_found(self):
        old = self._em(from_email='rare@vendor.com', subject='Kalibrierung',
                       sent_at=datetime(2025, 1, 1, tzinfo=dt_tz.utc))
        for i in range(60):
            self._em(from_email=f'p{i}@example.org', subject=f'News {i}')
        first_page = self.client.get('/email/', {'folder': 'inbox'})
        self.assertNotContains(first_page, f'id="row-{old.pk}"')
        r = self.client.get('/email/', {'folder': 'inbox', 'q': 'kalibrier'})
        self.assertContains(r, f'id="row-{old.pk}"')
        self.assertContains(r, 'id="em-p2-q" value="kalibrier"')
        self.assertContains(r, 'id="em-p2-pag"')
        # отримувач теж ключ пошуку (Надіслані)
        sent = self._em(folder='sent', imap_folder_name='Gesendete Objekte', from_email='viacheslav.pryimak@sevskiy.de',
                        to_emails=['orders@zulieferer.de'], subject='Bestellung')
        self.assertContains(self.client.get('/email/', {'q': 'zulieferer'}), f'id="row-{sent.pk}"')


class EmlImportTests(QuickFilterAllMailTests):
    """Імпорт .eml: кілька файлів без UID не конфліктують, видно у Вхідних, відповідь лягає в гілку."""

    def _eml(self, mid, subject, in_reply_to=''):
        head = f'Message-ID: {mid}\r\nFrom: Buyer <buyer@client.com>\r\nTo: viacheslav.pryimak@sevskiy.de\r\n'
        if in_reply_to:
            head += f'In-Reply-To: {in_reply_to}\r\nReferences: {in_reply_to}\r\n'
        return (head + f'Subject: {subject}\r\nDate: Mon, 5 Oct 2026 10:00:00 +0200\r\n\r\nHello\r\n').encode()

    def test_zip_of_two_emls(self):
        import io
        import zipfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('a.eml', self._eml('<e1@client.com>', 'Quote request'))
            zf.writestr('b.eml', self._eml('<e2@client.com>', 'AW: Quote request', in_reply_to='<e1@client.com>'))
        r = self.client.post('/email/import/', {'file': SimpleUploadedFile('mails.zip', buf.getvalue())})
        self.assertEqual(r.json(), {'ok': True, 'created': 2})
        a, b = (EmailMessage.objects.get(message_id=m) for m in ('<e1@client.com>', '<e2@client.com>'))
        self.assertEqual(a.thread_id, b.thread_id)
        inbox = self.client.get('/email/', {'folder': 'inbox'})
        self.assertContains(inbox, f'data-pk="{b.pk}"')
