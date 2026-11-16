"""Скринька відкривається швидко: лічильники лівої колонки й папок — менше проходів по листах (ті самі числа, що
раніше рахували окремими запитами), рядки списку без тіл листів, кількість запитів не залежить від кількості
листів на сторінці. python manage.py test email_assistant --settings=tabele.settings_test"""
from django.db import connection
from django.test.utils import CaptureQueriesContext

from email_assistant import views, work_categories
from email_assistant.importance import folder_q
from email_assistant.models import EmailMessage, EmailThread
from email_assistant.tests_work_categories import ME, Base
from offers.models import QuoteRequest


def old_unread(account):
    """Як рахувала inbox_view до 11.2026 (три окремі запити)."""
    archived = EmailThread.objects.filter(account=account, is_archived=True).values_list('id', flat=True)
    return {
        'inbox': EmailMessage.objects.filter(account=account, folder='inbox', is_read=False, is_deleted=False)
        .filter(views._inbox_q(account)).exclude(thread_id__in=archived).count(),
        'starred': EmailMessage.objects.filter(folder_q(), account=account, is_read=False, is_deleted=False).count(),
        'spam': EmailMessage.objects.filter(account=account, is_spam=True, is_read=False, is_deleted=False).count(),
    }


def old_work_counts(qs):
    from django.db.models import Count, Q
    through = EmailMessage.work_categories.through
    return {r['mailcategory']: (r['n'], r['unread']) for r in
            through.objects.filter(emailmessage__in=qs.order_by().values('pk')).values('mailcategory')
            .annotate(n=Count('emailmessage'), unread=Count('emailmessage', filter=Q(emailmessage__is_read=False)))}


class CountsTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        mk = self.mail
        self.normal = mk('a@client.example', subject='One')
        self.no_thread = mk('b@client.example', subject='Two')
        self.archived = mk('c@client.example', subject='Three')
        self.eml = mk('d@client.example', subject='Four')
        self.custom = mk('e@client.example', subject='Five')
        self.spam = mk('f@spam.example', subject='Six')
        self.star = mk('g@client.example', subject='Seven')
        self.important_read = mk('h@client.example', subject='Eight')
        self.deleted = mk('i@client.example', subject='Nine')
        self.sent = mk(ME, subject='Re: One', to=['a@client.example'], folder='Sent')
        self.dk = mk('orders@digikey.de', subject='DigiKey order')
        EmailMessage.objects.filter(account=self.account).update(is_read=False)
        EmailMessage.objects.filter(pk=self.no_thread.pk).update(thread=None)
        EmailThread.objects.filter(pk=self.archived.thread_id).update(is_archived=True)
        EmailMessage.objects.filter(pk=self.eml.pk).update(imap_folder_name='eml:import-1')
        EmailMessage.objects.filter(pk=self.custom.pk).update(imap_folder_name='Projekte')
        EmailMessage.objects.filter(pk=self.spam.pk).update(is_spam=True)
        EmailMessage.objects.filter(pk=self.star.pk).update(is_starred=True)
        EmailMessage.objects.filter(pk=self.important_read.pk).update(importance='urgent', is_read=True)
        EmailMessage.objects.filter(pk=self.deleted.pk).update(is_deleted=True)
        EmailMessage.objects.filter(pk=self.sent.pk).update(is_read=True)

    def test_unread_counts_as_before(self):
        self.assertEqual(views._unread_counts(self.account), old_unread(self.account))
        self.assertEqual(views._unread_counts(self.account)['inbox'], 6)   # без архівної гілки, своєї папки, видаленого

    def test_quote_counts_as_before(self):
        QuoteRequest.objects.create(message=self.normal, status=QuoteRequest.NEW)
        QuoteRequest.objects.create(message=self.no_thread, status=QuoteRequest.DONE)
        QuoteRequest.objects.create(message=self.star, status=QuoteRequest.ANSWERED)
        QuoteRequest.objects.create(message=self.spam, status=QuoteRequest.NEW)        # спам — поза лічильником
        QuoteRequest.objects.create(message=self.eml, status=QuoteRequest.DISMISSED)
        counts_qs = views._build_qs(self.account, views.ALL_FOLDERS)
        new = counts_qs.filter(pk__in=views._quote_messages(self.account)).count()
        done = counts_qs.filter(pk__in=views._quote_messages(self.account, with_done=True)).count() - new
        self.assertEqual(views._quote_counts(counts_qs), (new, done))
        self.assertEqual(views._quote_counts(counts_qs), (1, 2))

    def test_no_category_counted_in_the_same_pass(self):
        counts_qs = views._build_qs(self.account, views.ALL_FOLDERS)
        categories, customers, no_cat = views._category_counts(counts_qs, with_no_category=True)
        self.assertEqual(no_cat, counts_qs.filter(work_category=None).count())
        self.assertEqual((categories, customers), views._category_counts(counts_qs))

    def test_work_category_counts_as_before(self):
        self.dk.refresh_from_db()
        self.assertTrue(self.dk.work_categories.exists())
        work_categories.set_for_message(self.normal, self.shop, 'thread', 'add')     # лист у двох категоріях
        for qs in (views._build_qs(self.account, views.ALL_FOLDERS), views._build_qs(self.account, 'inbox')):
            new = {c.pk: (n, unread) for c, n, unread in work_categories.counts(qs) if n}
            self.assertEqual(new, old_work_counts(qs))

    def test_page_shows_the_same_counters(self):
        r = self.client.get('/email/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context['unread_counts']['inbox'], old_unread(self.account)['inbox'])
        self.assertEqual(r.context['unread_counts']['spam'], 1)


class ListSpeedTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def page_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            r = self.client.get('/email/')
        self.assertEqual(r.status_code, 200)
        return len(ctx.captured_queries), r

    def test_row_text_without_loading_bodies(self):
        long = 'Sehr geehrte Damen und Herren, ' * 30
        self.mail('a@client.example', subject='Long', body=long)
        _n, r = self.page_queries()
        row = r.context['emails'][0]
        self.assertNotIn('body_html', row.__dict__)                      # тіла листа список не читає
        self.assertNotIn('body_text', row.__dict__)
        from django.template.defaultfilters import truncatechars
        self.assertIn(truncatechars(long, 220), r.content.decode())

    def test_queries_do_not_grow_with_messages(self):
        for i in range(3):
            self.mail(f'c{i}@client{i}.example', subject=f'Mail {i}')
        self.page_queries()
        few, _ = self.page_queries()
        for i in range(3, 25):
            self.mail(f'c{i}@client{i}.example', subject=f'Mail {i}')
        many, r = self.page_queries()
        self.assertEqual(len(r.context['emails']), 25)
        self.assertEqual(few, many)


class ServerFoldersTests(Base):
    """«Папки сервера»: скринька питає їх при кожному відкритті — з кешу одразу, IMAP — лише вперше, у фоні для
    старого переліку і після змін папок."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from core.shared_cache import cache
        cache.clear()
        self.client.force_login(self.user)
        self.logins = 0
        self.folders = [{'name': 'Projekte', 'label': 'Projekte', 'selectable': True}]
        test = self

        class FakeIMAP:
            def __init__(self, account):
                test.logins += 1

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def list_folders(self):
                return list(test.folders)

            def create_folder(self, name):
                test.folders.append({'name': name, 'label': name, 'selectable': True})
                return True
        p = mock.patch('email_assistant.imap_client.IMAPClient', FakeIMAP)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch('email_assistant.sync._start_background', lambda fn: fn())   # фонове — одразу
        q.start()
        self.addCleanup(q.stop)

    def names(self):
        return [f['name'] for f in self.client.get('/email/imap-folders/').json()['folders']]

    def test_cached_after_first_and_refreshed_when_old(self):
        from core.shared_cache import cache
        self.assertEqual(self.names(), ['Projekte'])
        self.assertEqual(self.names(), ['Projekte'])
        self.assertEqual(self.logins, 1)                         # друге відкриття — без IMAP
        self.folders.append({'name': 'Lieferanten', 'label': 'Lieferanten', 'selectable': True})
        key = views._folders_key(self.account)
        old = cache.get(key)
        old['at'] -= views.FOLDERS_FRESH + 1
        cache.set(key, old)
        self.assertEqual(self.names(), ['Projekte'])             # одразу — що було, сервер перепитано у фоні
        self.assertEqual(self.logins, 2)
        self.assertEqual(self.names(), ['Projekte', 'Lieferanten'])
        self.assertEqual(self.logins, 2)

    def test_new_folder_shows_at_once(self):
        self.names()
        r = self.client.post('/email/imap-folder/create/', {'name': 'Neu'})
        self.assertTrue(r.json()['ok'])
        self.assertEqual(self.names(), ['Projekte', 'Neu'])


class LazyAttachmentLinksTests(Base):
    """Прев'ю листа з ще не прочитаним PDF відкривається одразу (без читання файлу й IMAP); номер замовлення з PDF
    дочитує окремий запит …/links/ і доповнює блок «🔗», наступне відкриття — уже з кешу."""

    def setUp(self):
        super().setUp()
        import os
        import shutil
        import tempfile
        from django.test import override_settings
        from email_assistant.tests_attachment_links import pdf_bytes
        from sales.models import SalesOrder
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        over = override_settings(MEDIA_ROOT=self.media)
        over.enable()
        self.addCleanup(over.disable)
        self.client.force_login(self.user)
        self.order = SalesOrder.objects.create(source='digikey', order_number='102497269', affects_stock=False)
        self.msg = self.mail('noreply@dakosy.de', subject='ABD - Ausfuhrbegleitdokument', body='Mit freundlichen Grüßen')
        rel = os.path.join('email_attachments', str(self.account.pk), 'ABD.pdf')
        os.makedirs(os.path.join(self.media, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(self.media, rel), 'wb') as fh:
            fh.write(pdf_bytes('Referenznummer/UCR [12 08] 102497269'))
        # файл з'явився вже після збереження листа (сигнал його не читав) — як старі листи до 06.11
        EmailMessage.objects.filter(pk=self.msg.pk).update(attachments=[
            {'name': 'ABD.pdf', 'file_path': rel, 'size': 100, 'content_type': 'application/pdf'}])

    def test_preview_first_then_links(self):
        url = f'/email/thread/{self.msg.thread_id}/preview/'
        html = self.client.get(url).content.decode()
        links_url = f'/email/thread/{self.msg.thread_id}/links/'
        self.assertIn(f'data-url="{links_url}"', html)
        self.assertNotIn('102497269', html)                         # PDF ще не читали
        self.assertNotIn('tokens', EmailMessage.objects.get(pk=self.msg.pk).attachments[0])
        d = self.client.get(links_url).json()
        self.assertIn('digikey:102497269', d['html'])
        self.assertIn('номер замовлення у вкладенні', d['html'])
        self.assertIn('102497269', EmailMessage.objects.get(pk=self.msg.pk).attachments[0]['tokens'])
        again = self.client.get(url).content.decode()               # тепер — одразу, без довантаження
        self.assertIn('digikey:102497269', again)
        self.assertNotIn('data-url=', again.split('id="pv-links-box"')[1][:40])

    def test_message_links_and_foreign_thread(self):
        d = self.client.get(f'/email/message/{self.msg.pk}/links/').json()
        self.assertIn('digikey:102497269', d['html'])
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user('bob', 'bob@x.example', 'x', is_staff=True)
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/email/thread/{self.msg.thread_id}/links/').status_code, 404)
