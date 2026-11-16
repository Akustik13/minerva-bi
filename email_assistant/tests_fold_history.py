"""Історія листування згорнута: у листах прев'ю й у формі відповіді; кнопки «Відправити / Пізніше / Зберегти» й угорі.
python manage.py test email_assistant --settings=tabele.settings_test"""
from django.test import SimpleTestCase

from email_assistant.html_clean import clean_html
from email_assistant.models import EmailAccount, EmailMessage
from email_assistant.quotes import new_text, split_quote
from email_assistant.tests_work_categories import Base

DUTCH = ("Hi Viacheslav,\n\nAlways good to plan a progress-call next week.\n\nRegards,\nPieter\n\n"
         "Van: Viacheslav <me@firma.example>\nVerzonden: vrijdag 13 november 2026 15:09\nAan: Pieter\n\nHi Pieter, ...")
OURS = ("Wednesday, 18/11, works well for us.\n\n--\nBest regards\n\n"
        "--- Оригінальний лист від p.oosthoek@wireless.example ---\nHi Viacheslav, old text")


class SplitQuoteTests(SimpleTestCase):
    def test_outlook_dutch_and_our_header(self):
        new, quote = split_quote(DUTCH)
        self.assertTrue(new.endswith('Pieter'))
        self.assertTrue(quote.startswith('Van: Viacheslav'))
        new, quote = split_quote(OURS)
        self.assertTrue(new.startswith('Wednesday') and 'Оригінальний' not in new)
        self.assertTrue(quote.startswith('--- Оригінальний лист'))
        self.assertEqual(new_text(OURS).strip().splitlines()[0], 'Wednesday, 18/11, works well for us.')   # AI теж
        self.assertNotIn('Verzonden', new_text(DUTCH))

    def test_reply_line_and_nothing_to_fold(self):
        text = 'Thanks, all clear.\nSee you.\n\nOn Fri, 13 Nov 2026 at 15:09 Pieter wrote:\n> old'
        self.assertEqual(split_quote(text)[1].splitlines()[0], 'On Fri, 13 Nov 2026 at 15:09 Pieter wrote:')
        self.assertEqual(split_quote('Just a letter\nwithout history'), ('Just a letter\nwithout history', ''))
        fwd = 'From: Customer <c@x.example>\nSent: Monday\n\nPlease send 100 pcs.'
        self.assertEqual(split_quote(fwd), (fwd, ''))   # переслано без коментаря — нічого не згортаємо
        self.assertEqual(split_quote('ok\n\nVon: X\nGesendet: Y'), ('ok\n\nVon: X\nGesendet: Y', ''))   # «ok» — замало

    def test_draft_keeps_quote_wrapper(self):
        self.assertIn('class="em-quote-in"', clean_html('<div class="em-quote-in"><blockquote>x</blockquote></div>'))


class FoldPagesTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.text_mail = self.mail('p.oosthoek@wireless.example', subject='RE: Indoor positioning', body=DUTCH)

    def test_text_letter_shows_history_folded(self):
        html = self.client.get(f'/email/message/{self.text_mail.pk}/preview/').content.decode()
        self.assertIn('<details class="em-qfold-t">', html)
        self.assertIn('Показати історію листування', html)
        self.assertIn('_pvFold', html)   # HTML-листи згортає сторінка

    def test_html_letter_is_not_split_on_server(self):
        EmailMessage.objects.filter(pk=self.text_mail.pk).update(body_html='<p>Hi</p>')
        html = self.client.get(f'/email/message/{self.text_mail.pk}/preview/').content.decode()
        self.assertNotIn('<details class="em-qfold-t">', html)

    def test_reply_form_folds_history_and_has_top_buttons(self):
        html = self.client.get('/email/compose/', {'reply_to': self.text_mail.pk}).content.decode()
        self.assertIn('<div class="em-quote-in">', html)
        self.assertIn('id="em-qbar"', html)
        self.assertIn('id="send-btn-top"', html)
        self.assertIn('id="schedule-toggle-btn-top"', html)
        new = self.client.get('/email/compose/').content.decode()
        self.assertNotIn('<div class="em-quote-in">', new)
        self.assertIn('id="send-btn-top"', new)   # кнопки вгорі — і в новому листі
