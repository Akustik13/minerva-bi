"""📱 Додаток «Minerva Пошта»: підключення QR-кодом, ключ телефону, API /email/phone/api/… (ті самі функції, що веб-пошта):
python manage.py test email_assistant.tests_mail_app --settings=tabele.settings_test

Пошта не надсилається (SMTPClient.send підмінено), IMAP не викликається (_imap_bg підмінено)."""
import base64
import datetime
import json
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from email_assistant import mail_app
from email_assistant.models import EmailAccount, EmailDraft, EmailMessage, EmailThread, MailAppDevice, MailCategory

ME = 'sales@sevskiy.example'
SEND = 'email_assistant.smtp_client.SMTPClient.send'
IMAP_BG = 'email_assistant.views._imap_bg'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==')


class Base(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp(prefix='minerva-mailapp-')
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        media = override_settings(MEDIA_ROOT=self.media)
        media.enable()
        self.addCleanup(media.disable)
        from core.shared_cache import cache
        cache.clear()
        from config.models import SystemSettings
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.user = User.objects.create_superuser('slava', 's@x.y', 'p')
        self.account = EmailAccount.objects.create(
            user=self.user, email_address=ME, imap_username=ME, imap_password='x', smtp_username=ME,
            smtp_password='x', imap_folder_inbox='INBOX', is_primary=True, signature='<p>Mit Grüßen<br>Slava</p>')
        self.other = User.objects.create_superuser('olena', 'o@x.y', 'p')
        self.other_account = EmailAccount.objects.create(
            user=self.other, email_address='olena@sevskiy.example', imap_username='o', imap_password='x',
            smtp_username='o', smtp_password='x', is_primary=True)
        from email_assistant.models import EmailSettings
        for u in (self.user, self.other):   # без справжніх IMAP-підключень у фоні («прочитано» на сервері)
            es = EmailSettings.get_for_user(u)
            es.mark_read_on_server = False
            es.save()
        self.uid = 0
        self.app = Client(enforce_csrf_checks=True)   # телефон: без сесії й CSRF — лише ключ

    def message(self, account=None, folder=EmailMessage.FOLDER_INBOX, **kw):
        self.uid += 1
        account = account or self.account
        data = dict(account=account, imap_uid=self.uid, imap_folder_name='INBOX', folder=folder,
                    message_id=f'<m{self.uid}@x>', subject='Anfrage FT220407', from_email='buyer@client.example',
                    from_name='Buyer GmbH', to_emails=[account.email_address], body_text='Hallo', is_read=False,
                    sent_at=datetime.datetime(2026, 10, 20, 9, self.uid, tzinfo=datetime.timezone.utc))
        data.update(kw)
        return EmailMessage.objects.create(**data)

    def token(self, user=None, name='Pixel 8'):
        _device, token = mail_app.pair(mail_app.new_pairing(user or self.user, 'https://m.example')['code'], name,
                                       '1.0.0', '10.0.0.5')
        return token

    def call(self, method, path, token, data=None, json_body=None, **extra):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}', 'HTTP_X_MINERVA_APP': '1.0.0', **extra}
        url = '/email/phone/api/' + path
        if json_body is not None:
            return getattr(self.app, method)(url, json.dumps(json_body), content_type='application/json', **headers)
        return getattr(self.app, method)(url, data or {}, **headers)


class PairingTests(Base):
    def test_page_shows_qr_and_pairs_once(self):
        self.client.force_login(self.user)
        page = self.client.get('/email/phone/')
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Пошта на телефоні')
        j = self.client.post('/email/phone/', {'action': 'pair'}).json()
        qr = json.loads(j['qr'])
        self.assertEqual(qr['minerva'], 'mail')
        self.assertEqual(qr['code'], j['code'])
        self.assertEqual(j['paste'], f"{qr['url']}#{j['code']}")   # «📋 Копіювати код» — без камери
        self.assertFalse(self.client.get('/email/phone/', {'state': j['key']}).json()['paired'])
        r = self.app.post('/email/phone/api/pair/', json.dumps({'code': qr['code'], 'device': 'Pixel 8',
                                                              'version': '1.0.0'}), content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content)
        token = r.json()['token']
        self.assertTrue(token.startswith('mvm_'))
        self.assertEqual(r.json()['account']['email'], ME)
        device = MailAppDevice.objects.get()
        self.assertNotIn(token, (device.token_hash, device.token_hint))   # у базі — лише SHA-256
        self.assertEqual(device.user, self.user)
        self.assertTrue(self.client.get('/email/phone/', {'state': j['key']}).json()['paired'])
        again = self.app.post('/email/phone/api/pair/', {'code': qr['code']})
        self.assertEqual(again.status_code, 410)
        self.assertEqual(MailAppDevice.objects.count(), 1)

    def test_no_mailbox_no_pairing(self):
        nobody = User.objects.create_user('ivan', 'i@x.y', 'p', is_staff=True)
        with self.assertRaises(mail_app.PairError) as e:
            mail_app.pair(mail_app.new_pairing(nobody, 'https://m.example')['code'])
        self.assertEqual(e.exception.status, 403)
        self.client.force_login(nobody)
        self.assertEqual(self.client.post('/email/phone/', {'action': 'pair'}).status_code, 400)

    def test_wrong_code(self):
        self.assertEqual(self.app.post('/email/phone/api/pair/', {'code': 'nope'}).status_code, 410)

    def test_revoke_stops_key(self):
        token = self.token()
        self.assertEqual(self.call('get', 'me/', token).status_code, 200)
        self.client.force_login(self.user)
        self.client.post('/email/phone/', {'action': 'revoke', 'id': MailAppDevice.objects.get().pk})
        self.assertEqual(self.call('get', 'me/', token).status_code, 401)

    def test_phone_unpairs_itself(self):
        token = self.token()
        self.assertEqual(self.call('post', 'unpair/', token).json(), {'ok': True})
        self.assertIsNotNone(MailAppDevice.objects.get().revoked_at)
        self.assertEqual(self.call('get', 'me/', token).status_code, 401)

    def test_other_user_cannot_revoke(self):
        self.token()
        staff = User.objects.create_user('petro', 'p@x.y', 'p', is_staff=True)
        self.client.force_login(staff)
        r = self.client.post('/email/phone/', {'action': 'revoke', 'id': MailAppDevice.objects.get().pk})
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(MailAppDevice.objects.get().revoked_at)

    def test_bad_or_missing_key(self):
        self.assertEqual(self.app.get('/email/phone/api/me/').status_code, 401)
        self.assertEqual(self.call('get', 'me/', 'mvm_wrong').status_code, 401)
        self.assertEqual(self.call('get', 'me/', 'mvc_cards-key').status_code, 401)

    def test_inactive_user_key_stops(self):
        token = self.token()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.call('get', 'me/', token).status_code, 401)


class ApiTests(Base):
    def setUp(self):
        super().setUp()
        self.tok = self.token()

    def test_me(self):
        j = self.call('get', 'me/', self.tok).json()
        self.assertEqual(j['account']['email'], ME)
        self.assertIn('Mit Grüßen', j['signature']['text'])
        self.assertEqual([f['key'] for f in j['folders']][:2], ['inbox', 'sent'])
        self.assertEqual(MailAppDevice.objects.get().app_version, '1.0.0')

    def test_list_only_own_mailbox(self):
        mine = self.message(subject='Mine')
        self.message(account=self.other_account, subject='Not mine')
        j = self.call('get', 'messages/', self.tok).json()
        self.assertEqual([i['id'] for i in j['items']], [mine.pk])
        row = j['items'][0]
        self.assertEqual(row['from'], 'Buyer GmbH')
        self.assertFalse(row['read'])
        self.assertEqual(row['pks'], [mine.pk])

    def test_search_category_and_pages(self):
        cat = MailCategory.objects.create(name='DigiKey', senders='digikey.com')
        dk = self.message(subject='Order 123', from_email='noreply@digikey.com')
        self.message(subject='Hello')
        from email_assistant.work_categories import assign
        assign(dk)
        j = self.call('get', 'messages/', self.tok, {'wcat': cat.pk}).json()
        self.assertEqual([i['id'] for i in j['items']], [dk.pk])
        self.assertEqual(j['items'][0]['categories'][0]['name'], 'DigiKey')
        j = self.call('get', 'messages/', self.tok, {'q': 'Order'}).json()
        self.assertEqual([i['id'] for i in j['items']], [dk.pk])
        for _ in range(mail_app.PAGE + 2):
            self.message()
        j = self.call('get', 'messages/', self.tok).json()
        self.assertTrue(j['has_next'])
        self.assertEqual(len(j['items']), mail_app.PAGE)
        j2 = self.call('get', 'messages/', self.tok, {'page': 2}).json()
        self.assertFalse(j2['has_next'])

    def test_grouped_by_thread(self):
        thread = EmailThread.objects.create(account=self.account, subject='Anfrage')
        a = self.message(thread=thread)
        b = self.message(thread=thread, is_read=True)
        j = self.call('get', 'messages/', self.tok, {'grouped': '1'}).json()
        self.assertEqual(len(j['items']), 1)
        self.assertEqual(j['items'][0]['id'], b.pk)
        self.assertEqual(sorted(j['items'][0]['pks']), sorted([a.pk, b.pk]))
        self.assertEqual(j['items'][0]['thread_unread'], 1)

    def test_conversation_marks_read_and_splits_quote(self):
        thread = EmailThread.objects.create(account=self.account, subject='Anfrage')
        first = self.message(thread=thread, body_text='Bitte Angebot für 35 Stück.\n\nDanke')
        last = self.message(thread=thread, body_text='Gibt es ein Update?\n\nAm 20.10.2026 schrieb Buyer:\n> Bitte',
                            cc_emails=['kollege@client.example'])
        j = self.call('get', f'messages/{first.pk}/', self.tok).json()
        self.assertEqual([m['id'] for m in j['messages']], [first.pk, last.pk])
        self.assertEqual(j['messages'][1]['text'], 'Gibt es ein Update?')
        self.assertIn('schrieb', j['messages'][1]['quote'])
        self.assertEqual(j['reply']['reply_to'], last.pk)
        self.assertEqual(j['reply']['to'], 'buyer@client.example')
        self.assertEqual(j['reply']['cc'], ['kollege@client.example'])
        self.assertFalse(EmailMessage.objects.filter(thread=thread, is_read=False).exists())

    def test_foreign_message_is_404(self):
        theirs = self.message(account=self.other_account)
        self.assertEqual(self.call('get', f'messages/{theirs.pk}/', self.tok).status_code, 404)
        self.assertEqual(self.call('post', f'messages/{theirs.pk}/star/', self.tok).status_code, 404)
        self.assertEqual(self.call('get', f'messages/{theirs.pk}/html/', self.tok).status_code, 404)

    def test_actions_are_the_web_functions(self):
        m = self.message()
        r = self.call('post', f'messages/{m.pk}/star/', self.tok)   # без CSRF — ключ телефону
        self.assertEqual(r.json(), {'ok': True, 'starred': True})
        self.call('post', f'messages/{m.pk}/importance/', self.tok, json_body={'level': 'urgent'})
        self.call('post', f'messages/{m.pk}/read/', self.tok)
        m.refresh_from_db()
        self.assertTrue(m.is_starred and m.is_read)
        self.assertEqual(m.importance, 'urgent')
        self.call('post', 'bulk/', self.tok, json_body={'action': 'unread', 'pks': [m.pk]})
        m.refresh_from_db()
        self.assertFalse(m.is_read)
        with patch(IMAP_BG):
            self.call('post', f'messages/{m.pk}/delete/', self.tok)
        m.refresh_from_db()
        self.assertEqual(m.folder, EmailMessage.FOLDER_TRASH)
        self.call('post', f'messages/{m.pk}/restore/', self.tok)
        m.refresh_from_db()
        self.assertEqual(m.folder, EmailMessage.FOLDER_INBOX)
        self.assertEqual(self.call('get', f'messages/{m.pk}/star/', self.tok).status_code, 405)

    def test_archive_and_unarchive(self):
        thread = EmailThread.objects.create(account=self.account, subject='Anfrage')
        m = self.message(thread=thread)
        self.call('post', 'bulk/', self.tok, json_body={'action': 'archive', 'pks': [m.pk]})
        self.assertEqual(self.call('get', 'messages/', self.tok).json()['items'], [])
        archived = self.call('get', 'messages/', self.tok, {'folder': 'archived'}).json()['items']
        self.assertEqual([i['id'] for i in archived], [m.pk])
        self.call('post', f'threads/{thread.pk}/unarchive/', self.tok)
        self.assertEqual(len(self.call('get', 'messages/', self.tok).json()['items']), 1)

    def test_reply_with_quote_and_attachment(self):
        m = self.message(subject='Anfrage', body_text='Bitte Angebot')
        with patch(SEND, return_value={'ok': True, 'message_id': '<reply@x>'}) as send:
            r = self.call('post', 'send/', self.tok, {
                'to': 'buyer@client.example', 'subject': '', 'body': 'Gerne!', 'reply_to_pk': m.pk,
                'with_quote': '1', 'attachments': SimpleUploadedFile('a.txt', b'hi', 'text/plain')})
        self.assertTrue(r.json()['ok'], r.content)
        kw = send.call_args.kwargs
        self.assertEqual(kw['subject'], 'Re: Anfrage')
        self.assertEqual(kw['reply_to_message'], m)
        self.assertIn('Bitte Angebot', kw['body_text'])      # історія листування під відповіддю
        self.assertEqual(kw['attachments'][0]['name'], 'a.txt')
        sent = EmailMessage.objects.get(folder='sent')
        self.assertEqual(sent.thread_id, EmailMessage.objects.get(pk=m.pk).thread_id)

    def test_compose_modes(self):
        m = self.message(subject='Anfrage', cc_emails=['kollege@client.example'],
                         attachments=[{'name': 'spec.pdf', 'content_type': 'application/pdf', 'size': 10},
                                      {'name': 'logo.png', 'content_type': 'image/png', 'content_id': '<l>'}])
        j = self.call('get', f'messages/{m.pk}/compose/', self.tok, {'mode': 'reply'}).json()
        self.assertEqual((j['to'], j['cc'], j['subject'], j['reply_to']), ('buyer@client.example', '', 'Re: Anfrage',
                                                                            m.pk))
        j = self.call('get', f'messages/{m.pk}/compose/', self.tok, {'mode': 'all'}).json()
        self.assertEqual(j['cc'], 'kollege@client.example')
        j = self.call('get', f'messages/{m.pk}/compose/', self.tok, {'mode': 'forward'}).json()
        self.assertEqual(j['subject'], 'Fwd: Anfrage')
        self.assertEqual([a['ref'] for a in j['attachments']], [f'msg:{m.pk}:0'])   # без картинки з підпису
        self.assertIn('Hallo', j['body'])

    def test_drafts_roundtrip(self):
        r = self.call('post', 'drafts/save/', self.tok, {'to': 'buyer@client.example', 'subject': 'Entwurf',
                                                         'body': 'Text'}).json()
        self.assertTrue(r['ok'], r)
        pk = r['draft_pk']
        j = self.call('get', 'messages/', self.tok, {'folder': 'drafts'}).json()
        self.assertEqual(j['kind'], 'drafts')
        self.assertEqual([d['id'] for d in j['items']], [pk])
        d = self.call('get', f'drafts/{pk}/', self.tok).json()
        self.assertEqual((d['subject'], d['body']), ('Entwurf', 'Text'))
        theirs = EmailDraft.objects.create(account=self.other_account, subject='Olena')
        self.assertEqual(self.call('get', f'drafts/{theirs.pk}/', self.tok).status_code, 404)
        self.assertTrue(self.call('post', f'drafts/{pk}/delete/', self.tok).json()['ok'])
        self.assertFalse(EmailDraft.objects.filter(pk=pk).exists())

    def test_html_inlines_cid_images(self):
        m = self.message(body_html='<html><head></head><body><img src="cid:logo1"> Hallo</body></html>',
                         attachments=[{'name': 'logo.png', 'content_type': 'image/png', 'content_id': '<logo1>',
                                       'is_inline': True}])
        with patch('email_assistant.attachments.attachment_bytes', return_value=PNG):
            r = self.call('get', f'messages/{m.pk}/html/', self.tok)
        self.assertEqual(r.status_code, 200)
        self.assertIn("script-src 'none'", r['Content-Security-Policy'])
        body = r.content.decode()
        self.assertIn('data:image/png;base64,', body)
        self.assertNotIn('/email/message/', body)
        plain = self.message(body_text='Nur Text')
        self.assertEqual(self.call('get', f'messages/{plain.pk}/html/', self.tok).status_code, 404)

    def test_nav_counts(self):
        self.message()
        self.message(is_read=True)
        j = self.call('get', 'nav/', self.tok).json()
        self.assertIn('categories', j)
        c = self.call('get', 'counts/', self.tok).json()
        self.assertEqual(c['inbox'], 1)

    def test_new_mail_for_background_check(self):
        """🔔 Фонова перевірка додатка: counts/?new_after=<pk> — нові непрочитані «Вхідних» від інших людей."""
        old = self.message(subject='Alt')
        new = self.message(subject='Neue Anfrage', from_name='Anna Kunde')
        self.message(subject='Gelesen', is_read=True)
        self.message(subject='Meine', from_email=ME)
        self.message(account=self.other_account, subject='Olena')
        c = self.call('get', 'counts/', self.tok, {'new_after': old.pk}).json()
        self.assertEqual(c['inbox'], 3)          # лічильник — як у скриньці; у сповіщення — без своїх листів
        self.assertEqual(c['new'],[{'pk': new.pk, 'from': 'Anna Kunde', 'subject': 'Neue Anfrage'}])
        self.assertGreaterEqual(c['latest'], new.pk)
        self.assertEqual(self.call('get', 'counts/', self.tok, {'new_after': c['latest']}).json()['new'], [])
        self.assertNotIn('new', self.call('get', 'counts/', self.tok).json())

    def test_translate_dictated_text(self):
        """🎤 → 🌐: переклад надиктованого — та сама функція, що «🌐» у формі відповіді веб-пошти."""
        with patch('email_assistant.ai_helper.translate_email', return_value='Vielen Dank!') as tr:
            j = self.call('post', 'translate-text/', self.tok, json_body={'text': 'Дуже дякую!', 'lang': 'de'}).json()
        self.assertEqual(j, {'ok': True, 'translation': 'Vielen Dank!', 'lang': 'de'})
        self.assertEqual(tr.call_args.args[:2], ('Дуже дякую!', 'de'))
        self.assertEqual(self.call('get', 'translate-text/', self.tok).status_code, 405)
        self.assertEqual(self.call('post', 'translate-text/', 'mvm_wrong', json_body={'text': 'x'}).status_code, 401)

    def test_reply_language_for_dictation(self):
        de = self.message(body_text='Sehr geehrte Damen und Herren, bitte senden Sie uns ein Angebot für die '
                                    'Antennen und die Filter. Mit freundlichen Grüßen')
        uk = self.message(subject='Запит', body_text='Добрий день! Надішліть, будь ласка, пропозицію на антени й '
                                                     'фільтри до кінця тижня. Дякую')
        self.assertEqual(self.call('get', f'messages/{de.pk}/compose/', self.tok).json()['lang'], 'de')
        self.assertEqual(self.call('get', f'messages/{uk.pk}/compose/', self.tok).json()['lang'], 'uk')

    def test_agent_window_data(self):
        m = self.message()
        j = self.call('get', f'messages/{m.pk}/agent/', self.tok).json()
        self.assertTrue(j['ok'])
        self.assertEqual(j['sender'], 'buyer@client.example')
        theirs = self.message(account=self.other_account)
        self.assertEqual(self.call('get', f'messages/{theirs.pk}/agent/', self.tok).status_code, 404)

    def test_ai_reply_uses_web_function(self):
        m = self.message()
        with patch('email_assistant.ai_helper.generate_reply', return_value='Gerne senden wir Ihnen…') as gen:
            j = self.call('post', f'messages/{m.pk}/ai-reply/', self.tok, {'prompt': 'kurz, freundlich'}).json()
        self.assertTrue(j['ok'], j)
        self.assertEqual(j['reply'], 'Gerne senden wir Ihnen…')
        self.assertEqual(j['to'], 'buyer@client.example')
        self.assertEqual(gen.call_args.kwargs['instruction'], 'kurz, freundlich')
