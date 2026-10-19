"""
Тести пошти ↔ CRM.  Запуск:  python manage.py test email_assistant --settings=tabele.settings_test
"""
import email as email_lib
from datetime import datetime, timezone as dt_tz
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from crm.models import Customer, CustomerTimeline
from email_assistant.crm_link import find_order, resolve, own_addresses
from email_assistant.imap_client import IMAPClient, _get_body, decode_mutf7, html_to_text, parse_addresses
from email_assistant.models import EmailAccount, EmailMessage, EmailSettings, EmailThread
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
