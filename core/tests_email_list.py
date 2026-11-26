"""✉️ Поле «кілька email-адрес» (core/email_list.py): перевірка адрес, «＋ З акаунтів», вигляд у формі."""
from django import forms
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase

from core import email_list


class FieldTests(TestCase):
    def test_clean_normalizes_and_rejects_bad(self):
        f = email_list.EmailListField()
        self.assertEqual(f.clean('a@x.de; b@y.com\nA@x.de  <c@z.org>'), 'a@x.de, b@y.com, c@z.org')
        self.assertEqual(f.clean(''), '')
        with self.assertRaises(ValidationError) as e:
            f.clean('a@x.de, wrong-address')
        self.assertIn('wrong-address', str(e.exception))

    def test_required(self):
        with self.assertRaises(ValidationError):
            email_list.EmailListField(required=True).clean(' , ')


class SuggestionTests(TestCase):
    def test_me_users_mailboxes_general_without_duplicates(self):
        from config.models import NotificationSettings
        from email_assistant.models import EmailAccount
        me = User.objects.create_user('me', 'me@firma.de', 'x', is_staff=True, first_name='Ich')
        User.objects.create_user('olena', 'olena@firma.de', 'x', is_staff=True, first_name='Olena')
        User.objects.create_user('gone', 'gone@firma.de', 'x', is_staff=True, is_active=False)   # вимкнений
        EmailAccount.objects.create(user=me, email_address='info@firma.de', display_name='Info',
                                    imap_username='x', imap_password='x', smtp_username='x', smtp_password='x')
        ns = NotificationSettings.get()
        ns.email_to = 'team@firma.de, olena@firma.de'
        ns.save()
        got = email_list.suggestions(me)
        self.assertEqual([(s['email'], s['kind']) for s in got], [
            ('me@firma.de', 'me'), ('olena@firma.de', 'user'), ('info@firma.de', 'mailbox'),
            ('team@firma.de', 'general')])
        self.assertEqual(got[2]['label'], 'Info')

    def test_widget_renders_chips_box_and_options(self):
        sug = [{'email': 'olena@firma.de', 'label': 'Olena <b>', 'kind': 'user'}]

        class F(forms.Form):
            to = email_list.EmailListField(suggestions=sug)

        html = str(F(initial={'to': 'a@x.de'})['to'])
        self.assertIn('data-mvel', html)
        self.assertIn('name="to"', html)
        self.assertIn('a@x.de</textarea>', html)
        self.assertIn('data-email="olena@firma.de"', html)
        self.assertIn('Olena &lt;b&gt;', html)                  # імена — екрановані
        self.assertIn('＋ З акаунтів', html)
        self.assertIn('mv_email_list.js', str(F().media))

    def test_formfield_from_model_field_caches_suggestions(self):
        from jlcpcb.models import JLCConfig
        req = RequestFactory().get('/')
        req.user = User.objects.create_user('u', 'u@firma.de', 'x', is_staff=True)
        f1 = email_list.formfield(JLCConfig._meta.get_field('notify_email_to'), req)
        f2 = email_list.formfield(JLCConfig._meta.get_field('notify_email_cc'), req)
        self.assertEqual((f1.label, f1.required), ('Кому (email)', False))
        self.assertEqual(f1.widget.suggestions, f2.widget.suggestions)
        self.assertEqual(f1.widget.suggestions, req._mv_email_suggestions)   # раз на запит
        self.assertEqual(f1.widget.suggestions[0]['email'], 'u@firma.de')
