"""🎙 Розшифровки нарад не йдуть агенту (Claude): search_history → format_history лишає лише фрагменти, які можна
передати AI (retrieval.for_ai), і каже, скільки фрагментів нарад не передано.

    python manage.py test agent.tests_meeting_boundary --settings=tabele.settings_test
"""
from django.contrib.auth.models import User
from django.test import TestCase

from agent import rag
from rag_assistant.models import RetrievalJob

SECRET = "GEHEIMES-TRANSKRIPT Rabatt 17 Prozent"


class FormatHistoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("anna", "a@x.y", "p", is_staff=True)

    def job(self, hits):
        return RetrievalJob.objects.create(user=self.user, idempotency_key=f"minerva:test:{len(hits)}",
                                           request={"query": "x"}, status=RetrievalJob.COMPLETED, hits=hits)

    def meeting_hit(self):
        return {"document_id": "document:41", "kind": "document", "text": SECRET, "ai_blocked": True,
                "url": "/rag/knowledge/41/", "subject": "🎙 Нарада 1/1", "document_kind": "🎙 нарада",
                "sent_at": "2026-11-05T10:00:00+01:00"}

    def test_meeting_fragments_are_not_given_to_the_agent(self):
        mail = {"document_id": "email:501", "kind": "email", "message_pk": 501, "sent_at": "2026-03-02T10:00:00Z",
                "from_email": "buyer@client.com", "subject": "Preis 2025", "text": "11,90 EUR vereinbart",
                "url": "/email/message/501/"}
        out = rag.format_history("Preis", self.job([self.meeting_hit(), mail]), 5)
        self.assertIn("11,90 EUR vereinbart", out)
        self.assertNotIn(SECRET, out)
        self.assertNotIn("/rag/knowledge/41/", out)
        self.assertIn("Фрагментів розшифровок нарад не передано (лише для людини): 1", out)

    def test_only_meeting_fragments_means_nothing_found(self):
        out = rag.format_history("Rabatt", self.job([self.meeting_hit()]), 5)
        self.assertIn("нічого не знайдено", out)
        self.assertNotIn(SECRET, out)
