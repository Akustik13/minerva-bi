"""🧩 «Це може зробити агент» у віконці «Minerva» (agent/suggest.py, rag_assistant/views.py `_msg`, guide/assistant.py
`route`): прохання зробити роботу → кнопка з заповненою формою завдання; питання «як / де» — ні.

python manage.py test agent.tests_suggest --settings=tabele.settings_test
"""
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User

from agent import suggest
from agent.tests_tasks import Base
from rag_assistant.models import RagConversation, RagMessage


class SuggestTests(Base):
    def test_tasks_and_questions(self):
        for text in ('Перебери даташити антен і внеси параметри на склад',
                     'Знайди вагу AN220213-01C в даташиті й внеси',
                     'Внеси прихід з цього Lieferschein на склад',
                     'Підготуй відправлення для замовлення 102497269',
                     'Можеш знайти даташити фільтрів і внести країну виробника?'):
            self.assertTrue(suggest.is_task(text), text)
        for text in ('Як внести вагу товару?', 'Де знайти даташит?', 'Яка потужність AN220213-01C?',
                     'Скільки товарів на складі?', 'Привіт', 'Покажи, де кнопка друку етикетки'):
            self.assertFalse(suggest.is_task(text), text)
        self.assertTrue(suggest.is_rule('На такі листи від DigiKey відповідай, що товар відправлено'))

    def test_link_prefills_form(self):
        s = suggest.suggest('Знайди вагу AN220213-01C і FT220407 в даташитах і внеси', self.user)
        self.assertEqual(s['kind'], 'task')
        url = urlparse(s['url'])
        self.assertEqual(url.path, '/admin/agent/agenttask/add/')
        q = parse_qs(url.query)
        self.assertEqual(q['ids'], [f'{self.ant.pk},{self.filt.pk}'])
        self.assertIn('Знайди вагу', q['instruction'][0])
        self.client.force_login(self.user)
        page = self.client.get(s['url'])
        self.assertContains(page, 'Знайди вагу AN220213-01C і FT220407 в даташитах і внеси')
        self.assertContains(page, f'<option value="{self.ant.pk}" selected>')

    def test_rule_and_rights(self):
        self.assertEqual(suggest.suggest('На такі листи відповідай сам', self.user)['url'], '/email/')
        clerk = User.objects.create_user('clerk', 'c@x.y', 'p')
        self.assertIsNone(suggest.suggest('Перебери даташити і внеси параметри', clerk))
        self.assertIsNone(suggest.suggest('Як внести параметри?', self.user))

    def test_route_task_to_guide(self):
        from guide.assistant import route
        mode, why = route('Знайди вагу AN220213-01C в даташиті й внеси', rag_ready=True)
        self.assertEqual(mode, 'guide')
        self.assertIn('агента', why)

    def test_widget_message_has_suggestion(self):
        c = RagConversation.objects.create(user=self.user, title='t')
        RagMessage.objects.create(conversation=c, role=RagMessage.ROLE_USER,
                                  content='Перебери даташити антен і внеси параметри на склад')
        RagMessage.objects.create(conversation=c, role=RagMessage.ROLE_USER, content='Як внести параметри?')
        from rag_assistant.views import _msg
        rows = [_msg(m, self.user) for m in c.messages.order_by('pk')]
        self.assertEqual(rows[0]['agent']['label'], '🧩 Підготувати завдання агенту')
        self.assertIsNone(rows[1]['agent'])
        self.assertNotIn('agent', _msg(c.messages.first()))          # без людини — без підказки
