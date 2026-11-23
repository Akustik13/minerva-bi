"""📬 Пошта й 📅 календар у MCP (agent/tools_mail.py, agent/toolsets.py): лише свої скриньки й події, лише через
🔒 особистий ключ (агент — сам власник), лист — пропозиція (відповідь з тієї самої скриньки), застосовує людина ✓.
Особисті ключі в адмінці й консолі API (api/admin.py, dashboard/api_views.py).

python manage.py test agent.tests_tools_mail --settings=tabele.settings_test
"""
import datetime
import json

from django.contrib.auth.models import User
from django.utils import timezone

from agent import decide, mcp, toolsets
from agent.models import AgentProposal, AgentTask
from agent.tests_tasks import Base
from api.models import APIKey

URL = '/api/v1/mcp/'


class MailToolsTests(Base):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        self.addCleanup(cache.clear)        # лічильник ліміту запитів ключа — не для наступних тестів
        from email_assistant.models import EmailAccount, EmailMessage
        self.box = EmailAccount.objects.create(user=self.user, email_address='slava@sevskiy.de', is_active=True)
        self.other = User.objects.create_superuser('sergey', 'sg@x.y', 'p')
        self.other_box = EmailAccount.objects.create(user=self.other, email_address='sergey@sevskiy.de',
                                                     is_active=True)
        now = timezone.now()
        self.mine = EmailMessage.objects.create(
            account=self.box, folder='inbox', imap_folder_name='INBOX', imap_uid=1, subject='Anfrage Antenne AN220213',
            from_email='max@kunde.de', from_name='Max', to_emails=['slava@sevskiy.de'], sent_at=now,
            body_text='Bitte Angebot für 35 Stück AN220213-01C.')
        self.foreign = EmailMessage.objects.create(
            account=self.other_box, folder='inbox', imap_folder_name='INBOX', imap_uid=1, subject='Geheim Antenne',
            from_email='boss@firma.de', to_emails=['sergey@sevskiy.de'], sent_at=now, body_text='Antenne intern')
        self.key = APIKey.objects.create(name='Claude Code', scopes=['agent:read', 'agent:write'],
                                         agent_user=self.user, owner=self.user, private=True)

    def rpc(self, method, params=None, session='', key=None):
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}
        headers = {'HTTP_AUTHORIZATION': f'Bearer {(key or self.key).key}'}
        if session:
            headers['HTTP_MCP_SESSION_ID'] = session
        return self.client.post(URL, json.dumps(body), content_type='application/json', **headers)

    def call(self, name, session='', **args):
        r = self.rpc('tools/call', {'name': name, 'arguments': args}, session=session)
        self.assertEqual(r.status_code, 200, r.content)
        res = r.json()['result']
        return res['content'][0]['text'], res['isError']

    def test_tools_listed_by_rights(self):
        names = mcp.names(self.user, self.key)
        for n in ('list_mail', 'search_mail', 'read_email', 'read_mail_attachment', 'propose_email',
                  'calendar_events', 'calendar_days', 'my_worktime'):
            if n != 'my_worktime':                                 # робочий час — лише з askDANTE
                self.assertIn(n, names)
        nobox = User.objects.create_superuser('nobox', 'n@x.y', 'p')
        names = set(t['name'] for t in toolsets.tools_for(nobox, personal=True))
        self.assertNotIn('search_mail', names)                     # без своєї скриньки — пошти немає
        self.assertIn('calendar_events', names)
        self.assertEqual([t['name'] for t in toolsets.tools_for(None)], [])

    def test_shared_key_gets_no_personal_data(self):
        """Спільний ключ (його бачать і можуть використати інші) — без пошти, календаря, робочого часу."""
        shared = APIKey.objects.create(name='Team', scopes=['agent:read', 'agent:write'], agent_user=self.user,
                                       owner=self.other)
        names = mcp.names(self.user, shared)
        for n in ('list_mail', 'search_mail', 'read_email', 'calendar_events', 'propose_email'):
            self.assertNotIn(n, names)
        self.assertIn('find_products', names)                     # робочі дані — як і раніше
        r = self.rpc('tools/call', {'name': 'search_mail', 'arguments': {'query': 'Antenne'}}, key=shared)
        self.assertIn('error', r.json())                            # навіть якщо клієнт знає назву
        # особистий ключ, але агент — інша людина: теж ні
        odd = APIKey.objects.create(name='Odd', scopes=['agent:read'], agent_user=self.user, owner=self.other,
                                    private=True)
        self.assertNotIn('search_mail', mcp.names(self.user, odd))

    def test_search_and_read_only_own_mailboxes(self):
        text, err = self.call('search_mail', query='Antenne')
        self.assertFalse(err)
        self.assertIn(f'#{self.mine.pk}', text)
        self.assertNotIn('Geheim', text)                           # чужа скринька — ні
        text, err = self.call('read_email', id=self.mine.pk)
        self.assertFalse(err)
        self.assertIn('35 Stück', text)
        self.assertIn('<<<ЛИСТ', text)
        text, err = self.call('read_email', id=self.foreign.pk)
        self.assertTrue(err)
        self.assertIn('немає у ваших скриньках', text)

    def test_reply_proposal_and_draft_from_same_mailbox(self):
        from email_assistant.models import EmailDraft
        r = self.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                    'clientInfo': {'name': 'claude-code'}})
        session = r['Mcp-Session-Id']
        text, err = self.call('propose_email', session=session, reply_to_id=self.mine.pk,
                              subject='AW: Anfrage Antenne', body='Guten Tag, Angebot folgt.')
        self.assertFalse(err, text)
        self.assertIn('/review/', text)
        p = AgentProposal.objects.get(kind=AgentProposal.MAIL)
        self.assertEqual((p.mail_to, p.data.get('reply_to')), ('max@kunde.de', self.mine.pk))
        self.assertEqual(p.task.status, AgentTask.REVIEW)
        self.assertEqual(decide.mail(p, self.user), '')            # ✓ людини → чернетка-відповідь
        draft = EmailDraft.objects.get()
        self.assertEqual((draft.account_id, draft.reply_to_id, draft.to_emails),
                         (self.box.pk, self.mine.pk, ['max@kunde.de']))
        # будь-яка адреса (людина бачить «Кому») — але не на чужий лист
        text, err = self.call('propose_email', session=session, to=['info@lieferant.de'], subject='Frage',
                              body='Lieferzeit?')
        self.assertFalse(err, text)
        text, err = self.call('propose_email', session=session, reply_to_id=self.foreign.pk, subject='x', body='y')
        self.assertTrue(err)

    def test_calendar_events_own_only(self):
        from calendar_app.models import CalendarEvent
        start = timezone.now() + datetime.timedelta(days=1)
        CalendarEvent.objects.create(user=self.user, title='Termin Kunde', start_at=start,
                                     event_type=CalendarEvent.TYPE_MEETING)
        CalendarEvent.objects.create(user=self.other, title='Privat Sergey', start_at=start)
        text, err = self.call('calendar_events')
        self.assertFalse(err)
        self.assertIn('Termin Kunde', text)
        self.assertNotIn('Privat Sergey', text)


class PrivateKeyAdminTests(Base):
    """🔒 Особистий ключ: повний ключ бачить і змінює лише власник; інші — замасковано, вимкнути можуть."""

    def setUp(self):
        super().setUp()
        self.admin2 = User.objects.create_superuser('admin2', 'a2@x.y', 'p')
        self.key = APIKey.objects.create(name='Мій Claude', scopes=['agent:read'], agent_user=self.user,
                                         owner=self.user, private=True)
        self.url = f'/admin/api/apikey/{self.key.pk}/change/'

    def test_owner_sees_full_key_others_do_not(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.url), self.key.key)
        self.client.force_login(self.admin2)
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, self.key.key)
        self.assertContains(r, 'Особистий ключ')
        r = self.client.post(self.url, {'name': 'hack', 'private': '', 'agent_user': self.admin2.pk})
        self.assertEqual(r.status_code, 403)                       # змінити чужий особистий — ні
        self.key.refresh_from_db()
        self.assertEqual((self.key.name, self.key.agent_user_id), ('Мій Claude', self.user.pk))
        r = self.client.post('/admin/api/apikey/', {'action': 'action_deactivate',
                                                   '_selected_action': [self.key.pk]})
        self.assertEqual(r.status_code, 302)
        self.key.refresh_from_db()
        self.assertFalse(self.key.is_active)                       # на випадок витоку — вимкнути можна

    def test_new_key_owner_and_private_agent_rule(self):
        self.client.force_login(self.admin2)
        r = self.client.post('/admin/api/apikey/add/', {'name': 'Особистий', 'is_active': 'on', 'private': 'on',
                                                        'scopes_input': ['agent:read'], 'agent_user': self.user.pk,
                                                        'log_mode': 'all'})
        self.assertEqual(r.status_code, 200)                       # агент — не власник → помилка форми
        self.assertContains(r, 'агент лише від імені власника')
        r = self.client.post('/admin/api/apikey/add/', {'name': 'Особистий', 'is_active': 'on', 'private': 'on',
                                                        'scopes_input': ['agent:read'], 'agent_user': self.admin2.pk,
                                                        'log_mode': 'all'})
        self.assertEqual(r.status_code, 302, r.content[:500])
        key = APIKey.objects.get(name='Особистий')
        self.assertEqual(key.owner_id, self.admin2.pk)
        self.assertTrue(key.personal_ok())

    def test_console_refuses_someone_elses_private_key(self):
        from dashboard.api_views import console_keys
        self.assertNotIn(self.key.pk, [k['id'] for k in console_keys(self.admin2)])
        self.assertIn(self.key.pk, [k['id'] for k in console_keys(self.user)])
        self.client.force_login(self.admin2)
        r = self.client.post('/dashboard/api/proxy/', json.dumps({
            'method': 'POST', 'url': '/api/v1/mcp/', 'auth': {'type': 'key', 'key_id': self.key.pk},
            'body': json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})}),
            content_type='application/json')
        self.assertEqual(r.status_code, 403)
