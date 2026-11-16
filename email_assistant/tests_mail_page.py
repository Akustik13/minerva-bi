"""«↗ Окремо» — лист на всю ширину з тими самими кнопками, AI і RAG, нові листи самі в списку, наступна сторінка
при гортанні. python manage.py test email_assistant --settings=tabele.settings_test"""
from django.contrib.auth import get_user_model

from email_assistant.models import EmailAccount, EmailMessage
from email_assistant.tests_work_categories import ME, Base


class MailPageTests(Base):
    def setUp(self):
        super().setUp()
        EmailAccount.objects.filter(pk=self.account.pk).update(is_primary=True)
        self.client.force_login(self.user)
        self.first = self.mail('pieter@wireless.example', subject='Indoor positioning', msg_id='<p1@x>')
        self.reply = self.mail(ME, subject='Re: Indoor positioning', to=['pieter@wireless.example'], folder='Sent',
                               msg_id='<p2@x>', reply_to='<p1@x>')

    def test_old_addresses_open_the_full_view(self):
        self.assertEqual(self.reply.thread_id, self.first.thread_id)
        r = self.client.get(f'/email/thread/{self.first.thread_id}/')
        self.assertRedirects(r, f'/email/?msg={self.reply.pk}&solo=1', fetch_redirect_response=False)
        r = self.client.get(f'/email/message/{self.first.pk}/')
        self.assertRedirects(r, f'/email/?msg={self.first.pk}&solo=1', fetch_redirect_response=False)
        other = get_user_model().objects.create_user('bob', 'bob@x.example', 'x', is_staff=True)
        foreign = EmailAccount.objects.create(user=other, email_address='bob@x.example', imap_username='bob',
                                              imap_password='x', smtp_username='bob', smtp_password='x')
        theirs = self.mail('a@b.example', subject='Bob only', account=foreign)
        self.assertEqual(self.client.get(f'/email/thread/{theirs.thread_id}/').status_code, 404)
        self.assertEqual(self.client.get(f'/email/message/{theirs.pk}/').status_code, 404)

    def test_solo_page_opens_the_whole_thread(self):
        html = self.client.get('/email/', {'msg': self.first.pk, 'solo': '1'}).content.decode()
        self.assertIn('em-app ', html)
        self.assertIn(' em-solo"', html)
        self.assertIn(f'var pk = {self.first.pk};', html)
        self.assertIn(f'previewMsg(pk, {self.first.thread_id}, row', html)   # уся гілка — як у прев'ю скриньки
        self.assertIn('<title>Indoor positioning — Minerva</title>', html)
        self.assertIn(f'href="/email/?msg={self.first.pk}">', html)                  # «← До всієї пошти»
        plain = self.client.get('/email/', {'msg': self.first.pk}).content.decode()
        self.assertNotIn(' em-solo"', plain)
        self.assertIn('previewMsg(pk, 0, row', plain)

    def test_preview_separate_button(self):
        html = self.client.get(f'/email/thread/{self.first.thread_id}/preview/').content.decode()
        self.assertIn(f'href="/email/?msg={self.reply.pk}&amp;solo=1"', html)

    def test_unread_count_reports_newest_mail(self):
        d = self.client.get('/email/unread-count/').json()
        self.assertEqual(d['latest'], EmailMessage.objects.order_by('-pk').first().pk)
        newer = self.mail('new@client.example', subject='Brand new')
        self.assertEqual(self.client.get('/email/unread-count/').json()['latest'], newer.pk)

    def test_next_page_marker_for_scroll(self):
        for i in range(31):
            self.mail(f's{i}@client.example', subject=f'Mail {i}')
        html = self.client.get('/email/').content.decode()
        self.assertIn('id="em-p2-next" hidden data-next="2"', html)
        self.assertIn('_p2More', html)
        last = self.client.get('/email/', {'page': 2}).content.decode()
        self.assertIn('id="em-p2-next" hidden data-next=""', last)

    def test_main_menu_counter_can_be_refreshed(self):
        html = self.client.get('/email/').content.decode()
        self.assertIn('window.mvMailBadge = function(n)', html)
        self.assertIn('window.mvMailBadge(d.count || 0)', html)
