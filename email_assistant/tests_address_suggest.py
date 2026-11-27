"""Підказки адрес для «Копія (CC)» / «Кому» (email_assistant/address_suggest.py, /email/address-suggest/,
static/admin/js/mv_addr.js): команда Minerva → учасники листування → ймовірно потрібні → решта; пошук за частиною
слова; чужа пошта не підказує; сторінка перевірки завдання агента.

python manage.py test email_assistant.tests_address_suggest --settings=tabele.settings_test
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from email_assistant import address_suggest as S


def flat(groups):
    return {g['key']: [i['email'] for i in g['items']] for g in groups}


class AddressSuggestTests(TestCase):
    def setUp(self):
        from config.models import SystemSettings
        from email_assistant.models import EmailAccount, EmailContact, EmailThread
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.me = User.objects.create_superuser('slava', 'slava@firma.example', 'p', first_name='Slava')
        self.sergey = User.objects.create_user('sergey', 'sergey@firma.example', 'p', first_name='Sergey',
                                               last_name='Sevskiy', is_staff=True)
        User.objects.create_user('gone', 'gone@firma.example', 'p', is_staff=True, is_active=False)
        self.acc = EmailAccount.objects.create(user=self.me, email_address='slava@firma.example', is_primary=True)
        EmailAccount.objects.create(user=self.sergey, email_address='info@firma.example')
        self.other_acc = EmailAccount.objects.create(user=self.sergey, email_address='sergey@firma.example')
        now = timezone.now()
        self.thread = EmailThread.objects.create(account=self.acc, subject='Antennen')
        self.msg = self.mail(self.thread, 'anna@kunde.example', 'Anna Muster', ['slava@firma.example'],
                             ['Bob Kunde <bob@kunde.example>', 'noreply@kunde.example'], now)
        self.mail(self.thread, 'slava@firma.example', '', ['anna@kunde.example'], ['carl@kunde.example'],
                  now - timedelta(hours=1))
        old = EmailThread.objects.create(account=self.acc, subject='Andere Sache')
        for days in (30, 60):          # Dora — завжди разом з Анною, але не в цій гілці
            self.mail(old, 'anna@kunde.example', 'Anna', ['slava@firma.example'], ['Dora Einkauf <dora@kunde.example>'],
                      now - timedelta(days=days))
        # чужа пошта (скринька Сергія): ні його гілки, ні його співрозмовники Славі не підказуються
        theirs = EmailThread.objects.create(account=self.other_acc, subject='Secret')
        self.secret = self.mail(theirs, 'anna@kunde.example', 'Anna', ['sergey@firma.example'],
                                ['eve@geheim.example'], now, account=self.other_acc)
        EmailContact.objects.create(user=self.me, email='zed@other.example', name='Zed Zulu', use_count=7)
        from crm.models import Customer
        Customer.objects.create(name='Klaus Kunde', email='klaus@kunde.example', company='Kunde GmbH')
        Customer.objects.create(name='Petra Meier', email='petra@weit.example', company='Weit AG')

    def mail(self, thread, frm, name, to, cc, when, account=None):
        from email_assistant.models import EmailMessage
        return EmailMessage.objects.create(account=account or self.acc, thread=thread, folder='inbox',
                                           imap_folder_name='INBOX', imap_uid=EmailMessage.objects.count() + 1,
                                           subject=thread.subject, from_email=frm, from_name=name, to_emails=to,
                                           cc_emails=cc, body_text='x', sent_at=when)

    def test_groups_in_order_without_query(self):
        groups = S.suggest(self.me, '', self.msg, to='anna@kunde.example')
        self.assertEqual([g['key'] for g in groups], ['team', 'thread', 'related', 'other'])
        got = flat(groups)
        team = got['team']
        self.assertIn('sergey@firma.example', team)
        self.assertIn('info@firma.example', team)
        self.assertEqual(team[-1], 'slava@firma.example')                 # ви — наприкінці
        self.assertNotIn('gone@firma.example', team)                      # вимкнений користувач — ні
        self.assertEqual(set(got['thread']), {'bob@kunde.example', 'carl@kunde.example'})
        self.assertNotIn('anna@kunde.example', sum(got.values(), []))     # «Кому» — не пропонуємо
        self.assertNotIn('noreply@kunde.example', sum(got.values(), []))
        self.assertEqual(got['related'][0], 'dora@kunde.example')         # разом з Анною в листах
        self.assertIn('klaus@kunde.example', got['related'])              # клієнт CRM тієї самої фірми
        self.assertEqual(got['other'], ['zed@other.example'])             # без запиту — адресна книга
        dora = next(i for g in groups for i in g['items'] if i['email'] == 'dora@kunde.example')
        self.assertIn('разом з адресатом у листах: 2', dora['why'])
        self.assertNotIn('eve@geheim.example', sum(got.values(), []))     # чужа скринька

    def test_search_part_of_word_and_several_words(self):
        got = flat(S.suggest(self.me, 'Sev', self.msg, to='anna@kunde.example'))
        self.assertEqual(got, {'team': ['info@firma.example', 'sergey@firma.example']})   # прізвище з профілю — і його скринька info@
        got = flat(S.suggest(self.me, 'meier weit', None))
        self.assertEqual(got, {'other': ['petra@weit.example']})          # обидва слова — в імені й фірмі
        got = flat(S.suggest(self.me, 'zulu', None))
        self.assertEqual(got, {'other': ['zed@other.example']})
        got = flat(S.suggest(self.me, 'carl', self.msg))
        self.assertEqual(got, {'thread': ['carl@kunde.example']})
        self.assertEqual(S.suggest(self.me, 'eve', None), [])            # відправник з чужої скриньки

    def test_foreign_message_is_ignored_and_exclude(self):
        got = flat(S.suggest(self.me, '', self.secret))
        self.assertNotIn('thread', got)
        got = flat(S.suggest(self.me, '', self.msg, to='anna@kunde.example',
                             exclude='Bob <bob@kunde.example>, sergey@firma.example'))
        self.assertEqual(got['thread'], ['carl@kunde.example'])
        self.assertNotIn('sergey@firma.example', got['team'])

    def test_view(self):
        self.client.force_login(self.me)
        r = self.client.get('/email/address-suggest/', {'q': 'dor', 'msg': self.msg.pk, 'to': 'anna@kunde.example'})
        self.assertEqual(flat(r.json()['groups']), {'related': ['dora@kunde.example']})
        r = self.client.get('/email/address-suggest/', {'msg': self.secret.pk})
        self.assertNotIn('thread', flat(r.json()['groups']))
        self.client.logout()
        self.assertEqual(self.client.get('/email/address-suggest/').status_code, 302)

    def test_agent_review_cc_has_suggestions(self):
        from agent.models import AgentProposal, AgentTask
        task = AgentTask.objects.create(owner=self.me, title='Лист', instruction='Напиши на anna@kunde.example',
                                        message=self.msg, status=AgentTask.REVIEW)
        AgentProposal.objects.create(task=task, kind=AgentProposal.MAIL, mail_to='anna@kunde.example',
                                     mail_subject='Angebot', mail_body='Hallo')
        self.client.force_login(self.me)
        page = self.client.get(f'/admin/agent/agenttask/{task.pk}/review/').content.decode()
        self.assertIn(f'data-mv-addr data-mv-addr-to="to" data-mv-addr-msg="{self.msg.pk}"', page)
        self.assertIn('/static/admin/js/mv_addr.js', page)
        add = self.client.get('/admin/agent/agenttask/add/').content.decode()
        self.assertIn('data-mv-addr-to="mail_to"', add)
        self.assertIn('admin/js/mv_addr.js', add)


class AddressFieldsEverywhereTests(TestCase):
    """Підказки адрес у всіх полях «Кому» / «Копія»: скрипт на кожній сторінці, поля форм адмінки — самі."""

    def setUp(self):
        from config.models import SystemSettings
        from email_assistant.models import EmailAccount
        SystemSettings.objects.update_or_create(pk=1, defaults={'is_onboarding_complete': True})
        self.me = User.objects.create_superuser('slava', 'slava@firma.example', 'p')
        self.acc = EmailAccount.objects.create(user=self.me, email_address='slava@firma.example', is_primary=True)
        self.client.force_login(self.me)

    def test_tag_field_rules(self):
        from django import forms
        f = S.tag_field('email_to', forms.CharField(widget=forms.Textarea))
        self.assertEqual(f.widget.attrs['data-mv-addr'], '')
        self.assertEqual(S.tag_field('mail_reply_to', forms.EmailField()).widget.attrs['data-mv-addr'], 'one')
        self.assertEqual(S.tag_field('team_emails', forms.CharField()).widget.attrs['data-mv-addr'], '')
        self.assertEqual(S.tag_field('customer_notify_cc', forms.CharField()).widget.attrs['data-mv-addr'], '')
        for name, field in (('in_reply_to', forms.CharField()), ('email', forms.EmailField()),   # власна адреса — ні
                            ('email_address', forms.CharField()), ('cc', forms.BooleanField()),
                            ('reply_to', forms.ModelChoiceField(queryset=User.objects.none()))):
            self.assertNotIn('data-mv-addr', S.tag_field(name, field).widget.attrs, name)

    def test_admin_settings_forms_are_tagged(self):
        from config.models import NotificationSettings
        ns, _ = NotificationSettings.objects.get_or_create(pk=1)
        page = self.client.get(f'/admin/config/notificationsettings/{ns.pk}/change/').content.decode()
        self.assertRegex(page, r'<textarea name="email_to"[^>]*data-mv-addr=""')
        self.assertIn('/static/admin/js/mv_addr.js', page)                     # скрипт — у base_site, на кожній сторінці
        page = self.client.get('/admin/agent/agentrule/add/').content.decode()
        self.assertRegex(page, r'name="cc"[^>]*data-mv-addr=""|data-mv-addr=""[^>]*name="cc"')
        self.assertRegex(page, r'name="forward_to"[^>]*data-mv-addr=""|data-mv-addr=""[^>]*name="forward_to"')

    def test_compose_and_calendar_fields(self):
        page = self.client.get('/email/compose/').content.decode()
        self.assertIn('data-mv-addr data-mv-addr-msg=""', page)                     # «Кому»
        self.assertIn('data-mv-addr data-mv-addr-to="to"', page)                    # CC
        self.assertNotIn('em-compose-contacts', page)                               # старі підказки прибрано
        page = self.client.get('/calendar/').content.decode()
        self.assertIn('id="invd-addr"', page)
        self.assertIn('data-mv-addr="pick"', page)                                  # учасник зустрічі — вибором
