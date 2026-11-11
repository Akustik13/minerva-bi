"""🎙 Розшифровки нарад не йдуть у хмарний AI з пошти: «📋 Підготувати відповідь» (reply_brief.rag_sources) і
AI-відповідь з фрагментами RAG (retrieval.facts_text) відкидають фрагменти, позначені retrieval.verify_hits як
ai_blocked; людина про це бачить рядок в етапі «RAG».

    python manage.py test email_assistant.tests_meeting_boundary --settings=tabele.settings_test
"""
from types import SimpleNamespace
from unittest.mock import patch

from email_assistant import reply_brief
from email_assistant.tests_reply_brief import ReplyBriefBase

SECRET = "GEHEIMES-TRANSKRIPT Rabatt 17 Prozent"


class MeetingHitsTests(ReplyBriefBase):
    def _job(self, hits):
        return SimpleNamespace(status='completed', COMPLETED='completed', is_terminal=True, hits=hits, message='',
                               poll_after_ms=0)

    def hits(self):
        return [{'document_id': 'document:41', 'kind': 'document', 'text': SECRET, 'ai_blocked': True,
                 'url': '/rag/knowledge/41/', 'subject': '🎙 Нарада 1/1', 'document_kind': '🎙 нарада',
                 'sent_at': '2026-11-05T10:00:00+01:00'},
                {'document_id': 'email:501', 'kind': 'email', 'message_pk': 501, 'thread_id': None,
                 'sent_at': '2026-03-02T10:00:00Z', 'from_email': 'buyer@client.com', 'subject': 'Preis 2025',
                 'text': 'Letztes Jahr 11,90 EUR vereinbart', 'url': '/email/message/501/'}]

    def test_prepare_drops_meeting_fragments_from_brief_prompt(self):
        msg = self.mail()
        with patch('rag_assistant.retrieval.available', return_value=True), \
                patch('rag_assistant.retrieval.start', return_value=self._job(self.hits())):
            b = self.prepare(msg, wish='яку ціну давали торік?')
        self.assertEqual(b['status'], 'done', b)
        prompt = self.ai.prompt('brief')
        self.assertIn('11,90 EUR vereinbart', prompt)
        self.assertNotIn(SECRET, prompt)
        self.assertNotIn(SECRET, str(b['sources']))
        detail = [s for s in b['stages'] if s['key'] == 'rag'][0]['detail']
        self.assertIn('розшифровок нарад не взято', detail)

    def test_rag_sources_directly(self):
        src = reply_brief.Sources()
        with patch('rag_assistant.retrieval.start', return_value=self._job(self.hits())):
            status, detail, _note = reply_brief.rag_sources(src, self.user, 'Preis', scope='test')
        self.assertEqual(status, 'done')
        self.assertEqual([i['url'] for i in src.items], ['/email/message/501/'])
        self.assertIn(': 1', detail)

    def test_ai_reply_facts_skip_meeting_fragments(self):
        from rag_assistant import retrieval
        from rag_assistant.models import RetrievalJob
        job = RetrievalJob.objects.create(user=self.user, idempotency_key='minerva:test:mail', request={'query': 'x'},
                                          status=RetrievalJob.COMPLETED, hits=self.hits())
        facts = retrieval.facts_text(job)
        self.assertIn('11,90 EUR', facts)
        self.assertNotIn(SECRET, facts)
