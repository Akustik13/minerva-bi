"""📎 Файли для зовнішнього агента (agent/files.py, agent/tools_files.py, api/files.py): знайти, завантажити,
передати в Minerva, вкласти в лист, покласти до замовлення; 🔎 RAG-ПК через MCP і REST API.

python manage.py test agent.tests_files --settings=tabele.settings_test
"""
import base64
import datetime
import json
import os
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from agent import decide, files
from agent.models import AgentFile, AgentProposal, AgentTask
from agent.tests_tasks import Base
from api.models import APIKey

URL = '/api/v1/mcp/'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')


def path_of(text, prefix):
    """Шлях посилання з відповіді інструмента (адреса — та, через яку підключився агент)."""
    from urllib.parse import urlparse
    url = next(w for w in text.split() if prefix in w and w.startswith('http'))
    return urlparse(url.strip('"')).path


class FilesBase(Base):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        self.addCleanup(cache.clear)
        from config.models import SystemSettings
        SystemSettings.objects.filter(pk=1).update(site_domain='minerva.example', site_protocol='https')
        from email_assistant.models import EmailAccount, EmailMessage
        from sales.models import SalesOrder
        self.box = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de', is_active=True)
        rel = f'email_attachments/{self.box.pk}/zeichnung.pdf'
        os.makedirs(os.path.join(settings.MEDIA_ROOT, os.path.dirname(rel)), exist_ok=True)
        with open(os.path.join(settings.MEDIA_ROOT, rel), 'wb') as fh:
            fh.write(b'%PDF-1.4 zeichnung')
        self.mail = EmailMessage.objects.create(
            account=self.box, folder='inbox', imap_folder_name='INBOX', imap_uid=0, subject='Zeichnung',
            from_email='max@kunde.de', to_emails=['slava@sevskiy.de'], sent_at=timezone.now(), body_text='Anbei.',
            attachments=[{'name': 'zeichnung.pdf', 'size': 18, 'content_type': 'application/pdf', 'file_path': rel}])
        self.other = User.objects.create_superuser('sergey', 'sg@x.y', 'p')
        self.other_box = EmailAccount.objects.create(user=self.other, email_address='sergey@sevskiy.de',
                                                     is_active=True)
        self.order = SalesOrder.objects.create(source='digikey', order_number='102497269', client='ACME',
                                               email='john@acme.example', currency='EUR',
                                               order_date=timezone.localdate())
        folder = os.path.join(settings.MEDIA_ROOT, 'orders', 'digikey', '102497269')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'Rechnung_102497269.pdf'), 'wb') as fh:
            fh.write(b'%PDF-1.4 rechnung')
        self.key = APIKey.objects.create(name='Claude Code', scopes=['agent:read', 'agent:write'],
                                         agent_user=self.user, owner=self.user, private=True)
        self.shared = APIKey.objects.create(name='Спільний', scopes=['agent:read', 'agent:write'],
                                            agent_user=self.user)

    def rpc(self, method, params=None, session='', key=None):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        headers = {'HTTP_AUTHORIZATION': f'Bearer {(key or self.key).key}'}
        if session:
            headers['HTTP_MCP_SESSION_ID'] = session
        return self.client.post(URL, json.dumps(body), content_type='application/json', **headers)

    def call(self, tool, key=None, session='', **args):
        r = self.rpc('tools/call', {'name': tool, 'arguments': args}, session=session, key=key)
        self.assertEqual(r.status_code, 200, r.content)
        res = r.json()['result']
        return res['content'][0]['text'], res['isError'], res['content'][1:]


class MCPFilesTests(FilesBase):
    def test_tools_listed_and_upload_needs_write(self):
        tools = {t['name']: t for t in self.rpc('tools/list').json()['result']['tools']}
        for name in ('list_files', 'get_file', 'read_file', 'upload_file', 'upload_link', 'propose_save_file'):
            self.assertIn(name, tools)
        self.assertTrue(tools['get_file']['annotations']['readOnlyHint'])
        self.assertFalse(tools['upload_file']['annotations']['readOnlyHint'])
        self.assertIn('attachments', tools['propose_email']['inputSchema']['properties'])
        reader = APIKey.objects.create(name='Лише читання', scopes=['agent:read'], agent_user=self.user)
        r = self.rpc('tools/call', {'name': 'upload_file', 'arguments': {'name': 'a.txt', 'text': 'x'}}, key=reader)
        self.assertEqual(r.status_code, 403)                 # передати файл — лише з agent:write
        self.assertFalse(AgentFile.objects.exists())

    def test_upload_text_list_and_read(self):
        text, err, _ = self.call('upload_file', name='Preise.csv', text='SKU;Preis\nAN220213-01C;12,50')
        self.assertFalse(err, text)
        f = AgentFile.objects.get()
        self.assertEqual((f.owner, f.api_key, f.name), (self.user, self.key, 'Preise.csv'))
        self.assertIn(f.ref, text)
        self.assertFalse(AgentTask.objects.exists())          # передати файл — не пропозиція
        text, err, _ = self.call('list_files', uploads=True)
        self.assertIn(f.ref, text)
        text, err, _ = self.call('read_file', ref=f.ref)
        self.assertIn('AN220213-01C;12,50', text)
        # файл видно лише власнику
        self.assertRaises(files.FileError, files.open_file, self.other, f.ref)

    def test_blocked_and_bad_uploads(self):
        text, err, _ = self.call('upload_file', name='setup.exe', content_base64=base64.b64encode(b'MZ').decode())
        self.assertTrue(err)
        self.assertIn('.exe', text)
        _, err, _ = self.call('upload_file', name='a.pdf', content_base64='не base64!')
        self.assertTrue(err)
        _, err, _ = self.call('upload_file', name='a.txt', text='x', url='https://example.com/a.txt')
        self.assertTrue(err)                                   # одне з трьох
        self.assertFalse(AgentFile.objects.exists())

    def test_get_file_image_and_signed_link(self):
        text, err, _ = self.call('upload_file', name='foto.png', content_base64=base64.b64encode(PNG).decode())
        ref = AgentFile.objects.get().ref
        text, err, extra = self.call('get_file', ref=ref)
        self.assertFalse(err, text)
        self.assertEqual(extra[0]['type'], 'image')           # картинку агент бачить одразу
        self.assertEqual(base64.b64decode(extra[0]['data']), PNG)
        link = path_of(text, '/api/v1/files/link/')
        r = self.client.get(link)                              # без ключа
        self.assertEqual(r.status_code, 200)
        self.assertEqual(b''.join(r.streaming_content) if r.streaming else r.content, PNG)
        self.assertEqual(r['X-Content-Type-Options'], 'nosniff')
        self.assertIn('attachment', r['Content-Disposition'])
        r = self.client.get(link[:-6] + 'xxxxx/')
        self.assertEqual(r.status_code, 404)                   # підпис не збігся
        self.key.is_active = False
        self.key.save()
        self.assertEqual(self.client.get(link).status_code, 404)
        # embed — сам файл у відповіді
        self.key.is_active = True
        self.key.save()
        _, _, extra = self.call('get_file', ref=ref, embed=True)
        self.assertEqual(extra[-1]['type'], 'resource')
        self.assertEqual(base64.b64decode(extra[-1]['resource']['blob']), PNG)

    def test_mail_attachments_only_with_personal_key(self):
        ref = f'msg:{self.mail.pk}:0'
        text, err, _ = self.call('list_files', mail=self.mail.pk)
        self.assertFalse(err, text)
        self.assertIn(ref, text)
        text, err, _ = self.call('list_files', key=self.shared, mail=self.mail.pk)
        self.assertTrue(err)                                   # спільний ключ — без пошти
        text, err, _ = self.call('get_file', key=self.shared, ref=ref)
        self.assertTrue(err)
        self.assertRaises(files.FileError, files.open_file, self.other, ref, True)   # чужа скринька

    def test_order_files_listed(self):
        text, err, _ = self.call('list_files', order='102497269')
        self.assertFalse(err, text)
        self.assertIn(f'order:{self.order.pk}:Rechnung_102497269.pdf', text)
        _, err, _ = self.call('read_file', ref=f'order:{self.order.pk}:../../secret.txt')
        self.assertTrue(err)                                   # лише файли теки замовлення


class MailAttachmentTests(FilesBase):
    def test_email_with_all_kinds_of_attachments(self):
        from email_assistant import attachments
        from email_assistant.models import EmailDraft
        r = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                    'clientInfo': {'name': 'claude-code'}})
        session = r['Mcp-Session-Id']
        self.call('upload_file', session=session, name='Angebot_ACME.pdf',
                  content_base64=base64.b64encode(b'%PDF-1.4 angebot').decode())
        up = AgentFile.objects.get()
        refs = [up.ref, f'order:{self.order.pk}:Rechnung_102497269.pdf', f'msg:{self.mail.pk}:0',
                'datasheet:AN220213-01C']
        text, err, _ = self.call('propose_email', session=session, to=['john@acme.example'], subject='Unterlagen',
                                 body='Anbei die Unterlagen.', attachments=refs)
        self.assertFalse(err, text)
        self.assertIn('Angebot_ACME.pdf', text)
        p = AgentProposal.objects.get(kind=AgentProposal.MAIL)
        self.assertEqual([a['ref'] for a in p.data['attachments']], refs)
        self.client.force_login(self.user)
        page = self.client.get(f'/admin/agent/agenttask/{p.task_id}/review/')
        self.assertContains(page, 'Angebot_ACME.pdf')
        r = self.client.get('/agent/file/', {'ref': up.ref})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(decide.mail(p, self.user), '')          # ✓ людини → чернетка з усіма вкладеннями
        draft = EmailDraft.objects.get()
        self.assertEqual(len(draft.attachments), 4)
        kept = [a for a in draft.attachments if a.get('ref')]
        self.assertEqual([a['ref'] for a in kept], [f'order:{self.order.pk}:Rechnung_102497269.pdf'])
        loaded, missing = attachments.load_items(self.box, draft.attachments)
        self.assertEqual(missing, '')
        self.assertEqual(sorted(a['name'] for a in loaded),
                         ['AN220213-01C-datasheet.pdf', 'Angebot_ACME.pdf', 'Rechnung_102497269.pdf',
                          'zeichnung.pdf'])

    def test_bad_attachment_refused(self):
        r = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {}})
        session = r['Mcp-Session-Id']
        from email_assistant.models import EmailMessage
        foreign = EmailMessage.objects.create(
            account=self.other_box, folder='inbox', imap_folder_name='INBOX', imap_uid=0, subject='x',
            from_email='a@b.c', sent_at=timezone.now(), attachments=[{'name': 'x.pdf', 'size': 1}])
        bad = [f'msg:{foreign.pk}:0']
        text, err, _ = self.call('propose_email', session=session, to=['john@acme.example'], subject='x', body='y',
                                 attachments=bad)
        self.assertTrue(err)
        self.assertFalse(AgentProposal.objects.filter(kind=AgentProposal.MAIL).exists())


class SaveFileTests(FilesBase):
    def test_propose_save_to_order_and_apply(self):
        r = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {}})
        session = r['Mcp-Session-Id']
        self.call('upload_file', session=session, name='Lieferschein.pdf',
                  content_base64=base64.b64encode(b'%PDF-1.4 ls').decode())
        ref = AgentFile.objects.get().ref
        text, err, _ = self.call('propose_save_file', session=session, ref=ref, order='102497269')
        self.assertFalse(err, text)
        p = AgentProposal.objects.get(kind=AgentProposal.FILE)
        folder = os.path.join(settings.MEDIA_ROOT, 'orders', 'digikey', '102497269')
        self.assertFalse(os.path.exists(os.path.join(folder, 'Lieferschein.pdf')))   # без ✓ — нічого
        out = decide.apply_selected(p.task, [p.pk], self.user)
        self.assertEqual(out['applied'], 1, out)
        with open(os.path.join(folder, 'Lieferschein.pdf'), 'rb') as fh:
            self.assertEqual(fh.read(), b'%PDF-1.4 ls')
        # той самий вміст удруге — не дублюється
        self.call('propose_save_file', session=session, ref=ref, order='102497269', name='Kopie.pdf')
        p2 = AgentProposal.objects.filter(kind=AgentProposal.FILE).exclude(pk=p.pk).get()
        decide.apply_selected(p2.task, [p2.pk], self.user)
        self.assertFalse(os.path.exists(os.path.join(folder, 'Kopie.pdf')))


class RestFilesTests(FilesBase):
    def auth(self, key=None):
        return {'HTTP_AUTHORIZATION': f'Token {(key or self.key).key}'}

    def test_upload_list_download(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        r = self.client.post('/api/v1/files/', {'file': SimpleUploadedFile('Plan.pdf', b'%PDF-1.4 plan')},
                             **self.auth())
        self.assertEqual(r.status_code, 201, r.content)
        ref = r.json()['files'][0]['ref']
        self.assertTrue(ref.startswith('upload:'))
        r = self.client.get('/api/v1/files/', {'uploads': '1'}, **self.auth())
        self.assertEqual(r.json()['files'][0]['ref'], ref)
        r = self.client.get('/api/v1/files/download/', {'ref': ref}, **self.auth())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b'%PDF-1.4 plan')
        r = self.client.get('/api/v1/files/text/', {'ref': f'order:{self.order.pk}:Rechnung_102497269.pdf'},
                            **self.auth())
        self.assertEqual(r.status_code, 200)
        reader = APIKey.objects.create(name='R', scopes=['agent:read'], agent_user=self.user)
        r = self.client.post('/api/v1/files/', {'file': SimpleUploadedFile('a.pdf', b'x')}, **self.auth(reader))
        self.assertEqual(r.status_code, 403)
        r = self.client.get('/api/v1/files/', {'mail': self.mail.pk}, **self.auth(self.shared))
        self.assertEqual(r.status_code, 400)                   # пошта — лише особистий ключ

    def test_upload_link_without_key(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        text, err, _ = self.call('upload_link')
        self.assertFalse(err, text)
        path = path_of(text, '/api/v1/files/upload/')
        r = self.client.post(path, {'file': [SimpleUploadedFile('a.pdf', b'%PDF a'),
                                             SimpleUploadedFile('b.csv', b'x;y')]})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual([f['name'] for f in r.json()['files']], ['a.pdf', 'b.csv'])
        self.assertEqual(AgentFile.objects.filter(owner=self.user, source=AgentFile.LINK).count(), 2)
        r = self.client.post(path[:-4] + 'zz/', {'file': SimpleUploadedFile('c.pdf', b'x')})
        self.assertEqual(r.status_code, 400)
        self.key.allowed_ips = '10.9.9.9'                    # «Дозволені IP-адреси» ключа діють і для посилання
        self.key.save()
        r = self.client.post(path, {'file': SimpleUploadedFile('d.pdf', b'%PDF d')})
        self.assertEqual(r.status_code, 400)
        self.assertIn('адреси', r.json()['message'])

    def test_media_folder_not_served(self):
        f = files.save(self.user, 'x.pdf', b'%PDF x')
        self.client.force_login(self.user)
        r = self.client.get(settings.MEDIA_URL + f.file.name)
        self.assertEqual(r.status_code, 404)                   # лише через агента / API / сторінку перевірки

    def test_cleanup_expired(self):
        f = files.save(self.user, 'old.pdf', b'%PDF old')
        path = f.file.path
        AgentFile.objects.filter(pk=f.pk).update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
        self.assertEqual(files.cleanup(), 1)
        self.assertFalse(AgentFile.objects.exists())
        self.assertFalse(os.path.exists(path))


class RagAccessTests(FilesBase):
    def test_history_in_mcp_only_with_personal_key(self):
        with mock.patch('agent.rag.history_ready', return_value=True), \
                mock.patch('agent.rag.datasheets_ready', return_value=True):
            mine = {t['name'] for t in self.rpc('tools/list').json()['result']['tools']}
            shared = {t['name'] for t in self.rpc('tools/list', key=self.shared).json()['result']['tools']}
        self.assertIn('search_history', mine)
        self.assertIn('search_datasheets', mine)
        self.assertNotIn('search_history', shared)
        self.assertIn('search_datasheets', shared)

    def test_rest_datasheets(self):
        result = {'products': [{'product': 'AN220213-01C', 'source': 'AN220213.pdf'}],
                  'sources': [{'source': 'AN220213.pdf', 'page': 1, 'text': 'Frequency 2.4 GHz SMA'}]}
        with mock.patch('agent.rag.run_datasheets', return_value=('antenna', result, 6)):
            r = self.client.get('/api/v1/rag/datasheets/', {'q': 'antenna'}, HTTP_AUTHORIZATION=f'Token {self.key.key}')
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertEqual(data['products'], [{'product': 'AN220213-01C', 'minerva_sku': 'AN220213-01C'}])
        self.assertEqual(data['fragments'][0]['page'], '1')

    def test_rest_history_needs_personal_key(self):
        r = self.client.get('/api/v1/rag/history/', {'q': 'Preis'}, HTTP_AUTHORIZATION=f'Token {self.shared.key}')
        self.assertEqual(r.status_code, 403)

    def test_datasheet_text_unchanged(self):
        from agent import rag
        result = {'products': [{'product': 'AN220213-01C', 'source': 'a.pdf'}, {'product': 'XYZ-1'}],
                  'sources': [{'source': 'a.pdf', 'page': 2, 'text': 'Gain 5 dBi'}]}
        text = rag.format_datasheets('antenna', result, 6)
        self.assertIn('Товари: AN220213-01C (у Minerva: AN220213-01C); XYZ-1 (немає в Minerva)', text)
        self.assertIn('[1] документ: a.pdf, стор. 2 · товар AN220213-01C\nGain 5 dBi', text)

