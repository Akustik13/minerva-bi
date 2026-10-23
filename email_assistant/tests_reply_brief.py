"""«📋 Підготувати відповідь» (бриф → лист): python manage.py test email_assistant --settings=tabele.settings_test"""
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone as dt_tz
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import override_settings

from email_assistant import ai_helper, reply_brief
from email_assistant.models import EmailMessage, ReplyBrief
from email_assistant.sync import save_message
from email_assistant.tests import CategoryBase

ME = 'viacheslav.pryimak@sevskiy.de'
BRIEF = ('ПИТАННЯ Й ПРОХАННЯ СПІВРОЗМОВНИКА\n1. Ціна ANT-1 на 500 шт → 12,50 EUR [#{n}]\n'
         'МОВА І ТОН\nнімецька, на Sie')


class FakeAI:
    """Підміна AI: бриф і лист розрізняються за текстом промпту; промпти й моделі зберігаються."""

    def __init__(self, brief=BRIEF, letter='Sehr geehrte Frau Muster,\nder Preis beträgt 12,50 EUR [#3].\n\n'
                                           'Mit freundlichen Grüßen\nSlava'):
        self.brief, self.letter, self.calls = brief, letter, []

    def __call__(self, prompt, max_tokens=1024, user=None, model=''):
        kind = 'brief' if 'БРИФ для листа-відповіді' in prompt else 'letter'
        self.calls.append({'kind': kind, 'prompt': prompt, 'model': model})
        ai_helper._last.model = model or 'claude-opus-5-5'
        return self.brief if kind == 'brief' else self.letter

    def prompt(self, kind):
        return [c for c in self.calls if c['kind'] == kind][-1]['prompt']


class ReplyBriefBase(CategoryBase):
    def setUp(self):
        super().setUp()
        from config.models import SystemSettings
        s = SystemSettings.objects.get_or_create(pk=1)[0]
        s.is_onboarding_complete = True
        s.save()
        self.client.force_login(self.user)
        self.media = tempfile.mkdtemp()
        self._media = override_settings(MEDIA_ROOT=self.media)
        self._media.enable()
        self.addCleanup(self._media.disable)
        self.addCleanup(shutil.rmtree, self.media, True)
        inline = patch('email_assistant.reply_brief._start_background', lambda fn: fn())
        inline.start()
        self.addCleanup(inline.stop)
        self.ai = FakeAI()
        ai = patch('email_assistant.ai_helper._call_ai_direct', self.ai)
        ai.start()
        self.addCleanup(ai.stop)
        self._n = 0

    def mail(self, subject='Anfrage ANT-1', body='Bitte Preis für 500 Stück ANT-1.', from_email='buyer@client.com',
             in_reply_to='', day=1, folder='INBOX', folder_type=None, attachments=()):
        self._n += 1
        return save_message(self.account, {
            'uid': self._n, 'message_id': f'<b{self._n}@x.test>', 'in_reply_to': in_reply_to, 'references': [],
            'subject': subject, 'from_email': from_email, 'from_name': '',
            'to_emails': [ME] if from_email != ME else ['buyer@client.com'], 'cc_emails': [],
            'body_text': body, 'body_html': '', 'attachments': list(attachments), 'is_read': False,
            'sent_at': datetime(2026, 10, day, 9, self._n, tzinfo=dt_tz.utc)}, folder, folder_type)

    def attach(self, msg, name, content: bytes):
        os.makedirs(os.path.join(self.media, 'email_attachments'), exist_ok=True)
        with open(os.path.join(self.media, 'email_attachments', name), 'wb') as fh:
            fh.write(content)
        EmailMessage.objects.filter(pk=msg.pk).update(attachments=[
            {'name': name, 'content_type': 'text/plain', 'size': len(content),
             'file_path': f'email_attachments/{name}'}])
        msg.refresh_from_db()

    def prepare(self, msg, wish='', mode='letter'):
        r = self.client.post(f'/email/message/{msg.pk}/ai-prepare/', json.dumps({'wish': wish, 'mode': mode}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200, r.content[:300])
        return self.client.get(f'/email/brief/{r.json()["id"]}/').json()


class PrepareTests(ReplyBriefBase):
    def test_prepare_gathers_project_offers_attachments_then_writes(self):
        from offers.services import add_line, new_offer
        from projects.services import create_project
        question = self.mail(body='Guten Tag, bitte Preis für 500 Stück ANT-1 und Lieferzeit.')
        self.attach(question, 'spec.txt', b'ANT-1 Datenblatt: 2.4 GHz, IP67, SMA')
        other = self.mail(subject='Muster ANT-1', body='Die Muster sind angekommen, danke.', day=2)
        project = create_project(name='Antennen 2026', customer=self.client_c, threads=[question.thread, other.thread])
        offer = new_offer(customer=self.client_c, user=self.user, project=project)
        add_line(offer, sku='ANT-1', qty=500, unit_price='12.50', lead_time='3 Wochen')

        b = self.prepare(question, wish='назви ціну й строк')
        self.assertEqual(b['status'], 'done', b)
        stages = {s['key']: s['status'] for s in b['stages']}
        self.assertEqual(stages, {'thread': 'done', 'minerva': 'done', 'projects': 'done', 'offers': 'done',
                                  'attachments': 'done', 'rag': 'skipped', 'brief': 'done', 'letter': 'done'})
        kinds = {s['kind'] for s in b['sources']}
        self.assertTrue({'mail', 'project', 'offer', 'attachment'} <= kinds, kinds)
        brief_prompt = self.ai.prompt('brief')
        for expected in ('назви ціну й строк', 'ANT-1 Datenblatt', offer.number, 'Muster sind angekommen',
                         f'Проєкт {project.code}', '3 Wochen', 'Lieferzeit'):
            self.assertIn(expected, brief_prompt)
        self.assertEqual(self.ai.calls[0]['model'], ai_helper.BRIEF_MODEL)        # бриф — окрема швидша модель
        self.assertIn('Ціна ANT-1', self.ai.prompt('letter'))                      # лист пишеться з брифу
        self.assertEqual(b['draft'], 'Sehr geehrte Frau Muster,\nder Preis beträgt 12,50 EUR.')   # без [#N] і підпису
        self.assertEqual(b['to'], 'buyer@client.com')
        self.assertTrue(any(u.startswith('Angebote:') for u in b['used']), b['used'])
        question.refresh_from_db()
        self.assertEqual(question.ai_reply_draft, b['draft'])

    def test_rewrite_uses_edited_brief_and_only_letter(self):
        msg = self.mail()
        b = self.prepare(msg)
        self.ai.letter = 'Neuer Text mit 5 % Rabatt.'
        r = self.client.post(f'/email/brief/{b["id"]}/rewrite/', json.dumps({'brief': 'МІЙ БРИФ: знижка 5 %'}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 200)
        b2 = self.client.get(f'/email/brief/{b["id"]}/').json()
        self.assertEqual((b2['status'], b2['draft']), ('done', 'Neuer Text mit 5 % Rabatt.'))
        stages = {s['key']: s['status'] for s in b2['stages']}
        self.assertEqual((stages['thread'], stages['letter']), ('done', 'done'))   # етапи збору лишились видимими
        self.assertIn('МІЙ БРИФ: знижка 5 %', self.ai.prompt('letter'))
        self.assertEqual(len([c for c in self.ai.calls if c['kind'] == 'brief']), 1)   # дані заново не збирались
        r = self.client.post(f'/email/brief/{b["id"]}/rewrite/', json.dumps({'brief': '  '}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_saved_brief_goes_stale_and_is_owner_only(self):
        msg = self.mail()
        b = self.prepare(msg)
        saved = self.client.get(f'/email/message/{msg.pk}/brief/').json()['brief']
        self.assertEqual((saved['id'], saved['stale']), (b['id'], False))
        self.mail(subject='AW: Anfrage ANT-1', body='Noch eine Frage', in_reply_to=msg.message_id, day=3)
        self.assertTrue(self.client.get(f'/email/message/{msg.pk}/brief/').json()['brief']['stale'])
        other = User.objects.create_superuser('other', 'o@x.y', 'p')
        self.client.force_login(other)
        self.assertEqual(self.client.get(f'/email/brief/{b["id"]}/').status_code, 404)
        self.assertEqual(self.client.post(f'/email/brief/{b["id"]}/rewrite/', json.dumps({'brief': 'x'}),
                                          content_type='application/json').status_code, 404)

    def test_ai_failure_is_reported(self):
        self.ai.brief = ''
        b = self.prepare(self.mail())
        self.assertEqual(b['status'], 'error')
        self.assertIn('бриф', b['error'])
        self.assertEqual({s['key']: s['status'] for s in b['stages']}['brief'], 'failed')


class StepsTests(ReplyBriefBase):
    def test_data_only_then_brief_then_letter_without_regathering(self):
        msg = self.mail(body='Bitte Preis für 500 Stück ANT-1.')
        self.mail(subject='Rechnung 2025', body='Die Rechnung 4711 ist bezahlt.', day=2)   # інше листування клієнта
        b = self.prepare(msg, wish='назви ціну', mode='data')
        self.assertEqual((b['status'], b['mode'], b['brief'], b['draft']), ('done', 'data', '', ''))
        self.assertEqual(self.ai.calls, [])                                   # лише дані — без AI
        stages = {s['key']: (s['status'], s['detail']) for s in b['stages']}
        self.assertEqual(stages['brief'][0], 'skipped')
        self.assertIn('кнопкою', stages['brief'][1])
        kinds = {s['kind'] for s in b['sources']}
        self.assertTrue({'mail', 'history', 'others'} <= kinds, kinds)
        others = [s for s in b['sources'] if s['kind'] == 'others'][0]
        self.assertIn('Rechnung 4711', others['text'])                          # повний текст для показу
        self.assertIn('Rechnung 2025', others['label'])

        with patch('email_assistant.reply_brief.gather', side_effect=AssertionError('no regather')):
            r = self.client.post(f'/email/brief/{b["id"]}/compose/', json.dumps({}), content_type='application/json')
            self.assertEqual(r.status_code, 200)
            b2 = self.client.get(f'/email/brief/{b["id"]}/').json()
            stages = {s['key']: s['status'] for s in b2['stages']}
            self.assertEqual((b2['status'], b2['draft'], stages['thread'], stages['brief'], stages['letter']),
                             ('done', '', 'done', 'done', 'skipped'))
            self.assertTrue(b2['brief'].startswith('ПИТАННЯ'))
            self.assertIn('Rechnung 4711', self.ai.prompt('brief'))           # бриф — зі збережених даних
            self.client.post(f'/email/brief/{b["id"]}/rewrite/', json.dumps({'brief': b2['brief']}),
                             content_type='application/json')
            b3 = self.client.get(f'/email/brief/{b["id"]}/').json()
        self.assertEqual((b3['status'], b3['draft']), ('done', 'Sehr geehrte Frau Muster,\nder Preis beträgt 12,50 EUR.'))
        self.assertEqual([c['kind'] for c in self.ai.calls], ['brief', 'letter'])

    def test_thread_letters_without_quotes_except_target(self):
        first = self.mail(body='Bitte Preis für 500 Stück ANT-1.')
        ours = self.mail(subject='AW: Anfrage ANT-1', from_email=ME, in_reply_to=first.message_id, day=2,
                         folder='Sent', folder_type='sent',
                         body='Der Preis beträgt 12,50 EUR.\n\nAm 01.10.2026 schrieb buyer@client.com:\n'
                              '> Bitte Preis für 500 Stück ANT-1.')
        target = self.mail(subject='AW: AW: Anfrage ANT-1', in_reply_to=ours.message_id, day=3,
                           body='Danke, und die Lieferzeit?\n\nVon: Viacheslav Pryimak\nGesendet: 02.10.2026\n'
                                'Der Preis beträgt 12,50 EUR.')
        self.assertEqual(len({first.thread_id, ours.thread_id, target.thread_id}), 1)
        b = self.prepare(target, mode='data')
        mail = {s['url']: s['text'] for s in b['sources'] if s['kind'] == 'mail'}
        self.assertEqual(mail[f'/email/message/{ours.pk}/'], f'Der Preis beträgt 12,50 EUR.\n{reply_brief.QUOTE_NOTE}')
        self.assertIn('Gesendet: 02.10.2026', mail[f'/email/message/{target.pk}/'])     # лист-адресат — повністю
        self.assertEqual(mail[f'/email/message/{first.pk}/'], 'Bitte Preis für 500 Stück ANT-1.')   # без цитати — як є
        self.assertEqual(b['dedup']['quote_letters'], 1)
        self.assertGreater(b['dedup']['quote_chars'], 30)
        self.assertIn('цитати прибрано в 1', [s for s in b['stages'] if s['key'] == 'thread'][0]['detail'])

    def test_brief_mode_stops_before_letter(self):
        b = self.prepare(self.mail(), mode='brief')
        self.assertEqual((b['status'], bool(b['brief']), b['draft']), ('done', True, ''))
        self.assertEqual([c['kind'] for c in self.ai.calls], ['brief'])

    def test_running_state_has_no_source_texts(self):
        msg = self.mail()
        brief = ReplyBrief.objects.create(user=self.user, message=msg, thread=msg.thread, status=ReplyBrief.RUNNING,
                                          sources=[{'n': 1, 'kind': 'mail', 'label': 'x', 'url': '', 'text': 'secret body'}])
        self.assertNotIn('text', reply_brief.as_json(brief)['sources'][0])      # легке опитування під час роботи
        brief.status = ReplyBrief.DONE
        self.assertEqual(reply_brief.as_json(brief)['sources'][0]['text'], 'secret body')

    def test_compose_needs_gathered_data(self):
        msg = self.mail()
        brief = ReplyBrief.objects.create(user=self.user, message=msg, thread=msg.thread, status=ReplyBrief.DONE)
        r = self.client.post(f'/email/brief/{brief.pk}/compose/', json.dumps({}), content_type='application/json')
        self.assertEqual(r.status_code, 400)


class RagScopeTests(ReplyBriefBase):
    def _job(self, hits):
        return SimpleNamespace(status='completed', COMPLETED='completed', is_terminal=True, hits=hits, message='',
                               poll_after_ms=0)

    def test_rag_searches_only_this_customer_with_wish(self):
        msg = self.mail()
        self.assertIsNotNone(EmailMessage.objects.get(pk=msg.pk).thread.crm_customer_id)
        hit = {'sent_at': '2026-03-02T10:00:00Z', 'from_email': 'buyer@client.com', 'subject': 'Preis 2025',
               'text': 'Letztes Jahr 11,90 EUR vereinbart', 'url': '/email/message/99/'}
        with patch('rag_assistant.retrieval.available', return_value=True), \
                patch('rag_assistant.retrieval.start', return_value=self._job([hit])) as start:
            b = self.prepare(msg, wish='яку ціну давали торік?')
        kw = start.call_args.kwargs
        self.assertEqual((kw['customer_id'], kw['project_id'], kw['exclude_thread_ids']),
                         (self.client_c.pk, None, [msg.thread_id]))
        self.assertTrue(start.call_args.args[1].startswith('яку ціну давали торік?'))
        self.assertIn('11,90 EUR vereinbart', self.ai.prompt('brief'))
        self.assertEqual({s['key']: s['status'] for s in b['stages']}['rag'], 'done')

    def test_rag_skips_sources_and_project_threads_and_merges_fragments(self):
        from projects.services import create_project
        msg = self.mail()
        older = self.mail(subject='Muster ANT-1', body='Muster sind angekommen.', day=2)
        project = create_project(name='Antennen 2026', customer=self.client_c, threads=[msg.thread, older.thread])

        def hit(pk, text, thread_id=None):
            return {'message_pk': pk, 'thread_id': thread_id, 'sent_at': '2026-03-02T10:00:00Z',
                    'from_email': 'buyer@client.com', 'subject': f'Lieferung {pk}', 'text': text,
                    'url': f'/email/message/{pk}/'}
        hits = [hit(older.pk, 'Muster sind angekommen.'),                    # уже в джерелах (лист проєкту)
                hit(501, 'Teil eins: Preis 11,90 EUR'), hit(501, 'Teil zwei: Lieferzeit 3 Wochen'),
                hit(501, 'Teil zwei: Lieferzeit 3 Wochen'),                  # той самий фрагмент удруге
                hit(502, 'Alter Projektbrief', thread_id=older.thread_id),   # гілка проєкту, хоч RAG мав її оминути
                hit(503, 'Rabatt 5 % ab 1000 Stück')]
        with patch('rag_assistant.retrieval.available', return_value=True), \
                patch('rag_assistant.retrieval.start', return_value=self._job(hits)) as start:
            b = self.prepare(msg, mode='data')
        kw = start.call_args.kwargs
        self.assertEqual(set(kw['exclude_thread_ids']), {msg.thread_id, older.thread_id})
        self.assertEqual(kw['limit'], reply_brief.RAG_REQUEST)
        rag = [s for s in b['sources'] if s['kind'] == 'rag']
        self.assertEqual([s['url'] for s in rag], ['/email/message/501/', '/email/message/503/'])
        self.assertIn('фрагментів 2', rag[0]['label'])
        self.assertEqual(rag[0]['text'], 'Teil eins: Preis 11,90 EUR\n[…]\nTeil zwei: Lieferzeit 3 Wochen')
        self.assertEqual((b['dedup']['rag_dropped'], b['dedup']['rag_merged']), (2, 2))   # 501: ще 1 фрагмент + повтор
        detail = [s for s in b['stages'] if s['key'] == 'rag'][0]['detail']
        self.assertIn('листів 2', detail)
        self.assertIn('відкинуто дублів 2', detail)
        self.assertEqual(project.code[:2], 'P-')

    def test_no_rag_without_customer_or_project(self):
        msg = self.mail(from_email='unknown@nowhere.test')
        with patch('rag_assistant.retrieval.available', return_value=True), \
                patch('rag_assistant.retrieval.start') as start:
            b = self.prepare(msg)
        start.assert_not_called()
        rag = [s for s in b['stages'] if s['key'] == 'rag'][0]
        self.assertEqual(rag['status'], 'skipped')
        self.assertIn('не визначено', rag['detail'])


class AttachmentTextTests(ReplyBriefBase):
    @staticmethod
    def _pdf(text: str) -> bytes:
        """Найпростіший PDF з одним рядком тексту (без reportlab)."""
        stream = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode()
        objects = [b'<</Type/Catalog/Pages 2 0 R>>', b'<</Type/Pages/Kids[3 0 R]/Count 1>>',
                   b'<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R'
                   b'/Resources<</Font<</F1 5 0 R>>>>>>',
                   b'<</Length %d>>stream\n' % len(stream) + stream + b'\nendstream',
                   b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>']
        out, offsets = bytearray(b'%PDF-1.4\n'), []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += b'%d 0 obj\n' % i + body + b'\nendobj\n'
        xref = len(out)
        out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
        out += b''.join(b'%010d 00000 n \n' % o for o in offsets)
        out += b'trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref)
        return bytes(out)

    def test_docx_and_pdf_text(self):
        import docx
        d = docx.Document()
        d.add_paragraph('Lieferzeit ANT-1: 3 Wochen')
        path = os.path.join(self.media, 'a.docx')
        d.save(path)
        self.assertIn('3 Wochen', reply_brief.attachment_text({'name': 'a.docx', 'file_path': 'a.docx'}))
        with open(os.path.join(self.media, 'b.pdf'), 'wb') as fh:
            fh.write(self._pdf('Angebot ANT-1 12.50 EUR'))
        self.assertIn('12.50 EUR', reply_brief.attachment_text({'name': 'b.pdf', 'file_path': 'b.pdf'}))
        self.assertEqual(reply_brief.attachment_text({'name': 'x.exe', 'file_path': 'b.pdf'}), '')
        self.assertEqual(reply_brief.attachment_text({'name': 'c.pdf', 'file_path': '../../etc/passwd'}), '')
