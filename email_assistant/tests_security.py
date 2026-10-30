"""Безпека пошти: чужий HTML у формі листа, вкладення з адреси Minerva, чужі скриньки в адмінці, дані в <script>,
повторна відправка запланованого листа, типи вкладень, фільтри спаму, документи Minerva у вкладеннях.

python manage.py test email_assistant.tests_security --settings=tabele.settings_test
Пошта не надсилається (SMTPClient.send підмінено)."""
import datetime
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from email_assistant import attachments
from email_assistant.html_clean import clean_html
from email_assistant.imap_client import safe_content_type
from email_assistant.models import EmailAccount, EmailDraft, EmailRule, ScheduledEmail
from email_assistant.tests_drafts import SEND, Base

XSS = ('<p>Hallo</p><img src=x onerror="alert(1)"><svg onload=alert(2)><circle/></svg>'
       '<a href="javascript:alert(3)">klick</a><iframe src="https://evil.example"></iframe>'
       '<div style="position:fixed;background:url(https://evil.example/x)" onclick="alert(4)">Text</div>'
       '<script>alert(5)</script><math><mtext><img src=x onerror=alert(6)></mtext></math>')
BAD = ('onerror', 'onload', 'onclick', 'javascript:', '<svg', '<iframe', '<script>alert', '<math', 'position:fixed',
       'evil.example')


def person(username, role='manager', **profile):
    user = User.objects.create_user(username, f'{username}@x.y', 'p', is_staff=True)
    user.profile.role = role
    for key, value in profile.items():
        setattr(user.profile, key, value)
    user.profile.save()
    return User.objects.get(pk=user.pk)


# ── 1. Цитата / чернетка у формі листа ──────────────────────────────────────────
class CleanHtmlTests(TestCase):
    def test_allow_list(self):
        out = clean_html(XSS)
        for bad in BAD:
            self.assertNotIn(bad, out)
        self.assertIn('<p>Hallo</p>', out)
        self.assertIn('<a>klick</a>', out)
        self.assertIn('Text', out)

    def test_keeps_mail_formatting_and_safe_links(self):
        out = clean_html('<table border="1"><tr><td colspan="2" style="color:red">A &amp; B</td></tr></table>'
                         '<a href="https://x.example/?a=1&b=2">x</a><img src="cid:logo"><a href="mailto:a@b.c">m</a>'
                         '<div class="em-sig-in other" id="body-editor">--</div>')
        self.assertIn('<td colspan="2" style="color:red">A &amp; B</td>', out)
        self.assertIn('href="https://x.example/?a=1&amp;b=2"', out)
        self.assertIn('<img src="cid:logo">', out)
        self.assertIn('href="mailto:a@b.c"', out)
        self.assertIn('<div class="em-sig-in">', out)          # підпис Minerva лишається, чужий id — ні
        self.assertNotIn('id=', out)

    def test_stray_end_tags_do_not_leave_the_editor(self):
        self.assertEqual(clean_html('a</div></div><b>b'), 'a<b>b</b>')
        self.assertEqual(clean_html('<html><head><title>T</title><body><p>x'), '<p>x</p>')


class ComposeQuoteTests(Base):
    def assert_clean(self, response):
        html = response.content.decode()
        body = html[html.index('id="body-editor"'):]
        body = body[:body.index('</div>\n') if '</div>\n' in body else len(body)]
        for bad in BAD:
            self.assertNotIn(bad, body, bad)

    def test_reply_forward_and_draft_are_sanitized(self):
        msg = self.message(body_html=XSS, body_text='Hallo')
        self.assert_clean(self.client.get(f'/email/compose/?reply_to={msg.pk}'))
        self.assert_clean(self.client.get(f'/email/compose/?forward={msg.pk}'))
        draft = EmailDraft.objects.create(account=self.account, subject='S', body_html=XSS)
        self.assert_clean(self.client.get(f'/email/compose/?draft={draft.pk}'))


# ── 2. Вкладення з адреси Minerva ─────────────────────────────────────────────
class AttachmentServeTests(Base):
    def serve(self, content_type, name='x', data=b'<html><script>alert(1)</script>', download=False):
        msg = self.message(attachments=[{'name': name, 'content_type': content_type, 'size': len(data)}])
        with patch('email_assistant.attachments.attachment_bytes', return_value=data):
            return self.client.get(f'/email/message/{msg.pk}/attachment/0/' + ('?download=1' if download else ''))

    def test_html_and_svg_are_downloaded_with_nosniff_and_sandbox(self):
        for ct in ('text/html', 'image/svg+xml', 'application/xhtml+xml', 'text/xml', 'image/<svg onload=x>'):
            r = self.serve(ct, name='evil".html')
            self.assertTrue(r['Content-Disposition'].startswith('attachment'), ct)
            self.assertEqual(r['Content-Type'], 'application/octet-stream', ct)
            self.assertEqual(r['X-Content-Type-Options'], 'nosniff')
            self.assertIn('sandbox', r['Content-Security-Policy'])

    def test_safe_types_inline(self):
        r = self.serve('image/png', data=b'\x89PNG\r\n\x1a\n')
        self.assertTrue(r['Content-Disposition'].startswith('inline'))
        self.assertEqual(r['Content-Type'], 'image/png')
        self.assertIn('sandbox', r['Content-Security-Policy'])
        r = self.serve('text/plain', data=b'hello')
        self.assertEqual(r['Content-Type'], 'text/plain; charset=utf-8')
        self.assertTrue(r['Content-Disposition'].startswith('inline'))
        r = self.serve('application/pdf', data=b'%PDF-1.4')
        self.assertTrue(r['Content-Disposition'].startswith('inline'))
        self.assertEqual(r['X-Content-Type-Options'], 'nosniff')
        r = self.serve('image/png', download=True)
        self.assertTrue(r['Content-Disposition'].startswith('attachment'))


# ── 3. Чужа скринька в адмінці ───────────────────────────────────────────────
class OwnMailboxAdminTests(Base):
    def post_scheduled(self, account, **extra):
        data = {'account': account.pk, 'to_raw': 'buyer@client.example', 'cc_raw': '', 'subject': 'S', 'body': 'B',
                'body_html': '', 'scheduled_at': (timezone.localtime() + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
                'status': ScheduledEmail.STATUS_PENDING, 'trigger': 'manual', **extra}
        return self.client.post('/admin/email_assistant/scheduledemail/add/', data)

    def test_cannot_schedule_from_colleague_mailbox_even_as_superuser(self):
        page = self.client.get('/admin/email_assistant/scheduledemail/add/')
        choices = [pk for pk, _ in page.context['adminform'].form.fields['account'].choices if pk]
        self.assertEqual([int(str(c)) for c in choices], [self.account.pk])   # лише своя скринька
        r = self.post_scheduled(self.other_account)
        self.assertEqual(r.status_code, 200)                               # форма з помилкою
        self.assertFalse(ScheduledEmail.objects.exists())
        r = self.post_scheduled(self.account)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(ScheduledEmail.objects.get().account, self.account)

    def test_rules_only_on_own_mailbox(self):
        data = {'account': self.other_account.pk, 'name': 'x', 'is_active': 'on', 'condition_field': 'from_email',
                'condition_op': 'contains', 'condition_value': 'a@b.c', 'action': 'mark_read', 'action_value': ''}
        r = self.client.post('/admin/email_assistant/emailrule/add/', data)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(EmailRule.objects.exists())
        foreign = EmailRule.objects.create(account=self.other_account, name='x', condition_value='a@b.c')
        self.assertEqual(self.client.get(f'/admin/email_assistant/emailrule/{foreign.pk}/change/').status_code, 302)
        r = self.client.post('/admin/email_assistant/emailrule/add/', {**data, 'account': self.account.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(EmailRule.objects.filter(account=self.account).count(), 1)

    def test_category_rule_admin_lists_only_own(self):
        from email_assistant.models import EmailCategoryRule
        EmailCategoryRule.objects.create(account=self.other_account, key='spy@x.example', category='customer')
        self.assertNotContains(self.client.get('/admin/email_assistant/emailcategoryrule/'), 'spy@x.example')


# ── 4. Дані в <script> ───────────────────────────────────────────────────────
class ScriptDataTests(Base):
    NAME = '</script><script>alert("crm")</script>'

    def test_crm_names_cannot_close_the_script(self):
        from crm.models import Customer
        Customer.objects.create(name=self.NAME, email='evil@client.example')
        for url in ('/email/compose/', '/admin/email_assistant/scheduledemail/add/'):
            html = self.client.get(url).content.decode()
            self.assertNotIn('<script>alert("crm")', html, url)
            self.assertIn('evil@client.example', html, url)          # але контакт є — у json_script

    def test_scheduled_body_is_sanitized_and_json_script(self):
        mail = ScheduledEmail.objects.create(account=self.account, subject='S', to_emails=['a@b.c'],
                                             body='</script><script>alert(7)</script>', body_html=XSS,
                                             scheduled_at=timezone.now() + timedelta(hours=1))
        html = self.client.get(f'/admin/email_assistant/scheduledemail/{mail.pk}/change/').content.decode()
        self.assertNotIn('<script>alert(7)', html)
        data = html[html.index('id="se-existing-html"'):]
        data = data[:data.index('</script>')]
        for bad in ('onerror', 'onload', '<svg', 'javascript:'):
            self.assertNotIn(bad, data)


# ── 7. Повторне збереження запланованого листа ────────────────────────────────
class ScheduledResaveTests(Base):
    def setUp(self):
        super().setUp()
        self.mail = ScheduledEmail.objects.create(account=self.account, subject='S', to_emails=['a@b.c'], body='B',
                                                  scheduled_at=timezone.now() - timedelta(minutes=1))

    def resave(self, status=ScheduledEmail.STATUS_PENDING):
        return self.client.post(f'/admin/email_assistant/scheduledemail/{self.mail.pk}/change/', {
            'account': self.account.pk, 'to_raw': 'a@b.c', 'cc_raw': '', 'subject': 'S2', 'body': 'B', 'body_html': '',
            'scheduled_at': timezone.localtime().strftime('%Y-%m-%dT%H:%M'), 'status': status, 'trigger': 'manual'})

    def test_page_opened_before_sending_does_not_requeue(self):
        with patch(SEND, return_value={'ok': True}) as send:
            call_command('send_scheduled', stdout=StringIO())
        self.assertEqual(send.call_count, 1)
        r = self.resave()                                  # стара сторінка: приховане поле досі «Очікує»
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Лист уже надіслано')
        self.mail.refresh_from_db()
        self.assertEqual((self.mail.status, self.mail.subject), (ScheduledEmail.STATUS_SENT, 'S'))
        self.assertIsNotNone(self.mail.sent_at)
        with patch(SEND, return_value={'ok': True}) as send:
            call_command('send_scheduled', stdout=StringIO())
        send.assert_not_called()                           # удруге не пішов

    def test_letter_being_sent_is_not_changed(self):
        ScheduledEmail.objects.filter(pk=self.mail.pk).update(sent_at=timezone.now())   # бронь send_scheduled
        self.assertContains(self.resave(), 'Лист уже надіслано')
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.subject, 'S')

    def test_race_between_form_check_and_save(self):
        from email_assistant import admin as mail_admin
        real = mail_admin.scheduled_locked
        calls = []

        def locked_later(status, sent_at):   # clean() бачить «Очікує», а до запису cron уже забронював лист
            calls.append(1)
            if len(calls) == 1:
                ScheduledEmail.objects.filter(pk=self.mail.pk).update(sent_at=timezone.now())
                return False
            return real(status, sent_at)
        with patch('email_assistant.admin.scheduled_locked', side_effect=locked_later):
            r = self.resave()
        self.assertEqual(r.status_code, 302)
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.subject, 'S')           # умовний UPDATE нічого не змінив
        self.assertIsNotNone(self.mail.sent_at)

    def test_pending_letter_still_editable_and_cannot_be_marked_sent(self):
        self.resave(status=ScheduledEmail.STATUS_SENT)
        self.mail.refresh_from_db()
        self.assertEqual((self.mail.subject, self.mail.status), ('S2', ScheduledEmail.STATUS_PENDING))
        self.resave(status=ScheduledEmail.STATUS_CANCELLED)
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.status, ScheduledEmail.STATUS_CANCELLED)


# ── 8. Тип вкладення ──────────────────────────────────────────────────────────
class ContentTypeTests(Base):
    def test_normalised_at_parse_time(self):
        import email as email_lib
        from email_assistant.imap_client import _get_attachments, _get_attachments_raw, persist_attachments
        self.assertEqual(safe_content_type('image/<svg onload=alert(1)>'), 'application/octet-stream')
        self.assertEqual(safe_content_type('Application/PDF; name="a.pdf"'), 'application/pdf')
        raw = (b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nhi\r\n'
               b'--b\r\nContent-Type: image/x"><img src=x onerror=alert(1)>\r\n'
               b'Content-Disposition: attachment; filename="a.png"\r\n\r\nxx\r\n--b--\r\n')
        msg = email_lib.message_from_bytes(raw)
        self.assertEqual(_get_attachments(msg)[0]['content_type'], 'application/octet-stream')
        parts = _get_attachments_raw(msg)
        self.assertEqual(parts[0]['content_type'], 'application/octet-stream')
        self.assertEqual(persist_attachments(1, 1, 'INBOX', [{'name': 'a', 'content_type': 'text/<b>', 'size': 0}])
                         [0]['content_type'], 'application/octet-stream')

    def test_tooltip_escapes_type_and_name(self):
        msg = self.message(attachments=[{'name': '<img src=x onerror=alert(1)>', 'content_type': 'image/png',
                                         'size': 3}])
        r = self.client.get(f'/email/message/{msg.pk}/preview/')
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertNotIn('<img src=x onerror=alert(1)>', html)            # у data-атрибутах — екрановано
        tip = html[html.index('window.attTipShow'):]
        tip = tip[:tip.index('_tip.innerHTML = html')]
        self.assertIn('_attTipEsc(name)', tip)                             # у HTML підказки — лише екрановано
        self.assertIn('_attTipEsc(ct)', tip)
        self.assertNotIn("name.replace(/</g,'&lt;')", tip)


# ── 9. Фільтри спаму ──────────────────────────────────────────────────────────
class SpamKeyTests(Base):
    def test_sender_key_rejects_markup(self):
        from email_assistant import spam
        self.assertEqual(spam.sender_key('a"<img src=x onerror=alert(1)>@evil.example'), '')
        self.assertEqual(spam.sender_key('"Bad" <x<y>@evil.example>'), '')
        self.assertEqual(spam.sender_key('Spam <promo@spam-shop.example>'), '@spam-shop.example')
        msg = self.message(from_email='a"<svg/onload=alert(1)>@evil.example')
        spam.mark(self.account, [msg])
        self.assertFalse(EmailRule.objects.filter(condition_value__contains='<').exists())

    def test_filter_list_escapes(self):
        html = self.client.get('/email/').content.decode()
        block = html[html.index('function loadSpamFilters'):]
        block = block[:block.index('function addSpamFilter')]
        self.assertIn('esc(r.condition_value)', block)
        self.assertIn('esc(r.name)', block)


# ── 10. Документи Minerva у вкладеннях ────────────────────────────────────────
class AttachRefPermissionTests(Base):
    def test_order_and_offer_need_permission(self):
        ref = self.order_doc()
        self.assertIsNotNone(attachments.load_ref(self.account, ref))           # суперкористувач — так
        clerk = person('clerk', 'warehouse', allowed_modules=['email_assistant'])
        acc = EmailAccount.objects.create(user=clerk, email_address='clerk@sevskiy.example', imap_username='c',
                                          imap_password='x', smtp_username='c', smtp_password='x')
        self.assertIsNone(attachments.load_ref(acc, ref))                      # без «Продажів» — ні
        with patch('offers.services.revision_attachment', return_value={'name': 'A.pdf', 'content': b'x',
                                                                         'content_type': 'application/pdf'}):
            self.assertIsNone(attachments.load_ref(acc, 'offer:1'))
            self.assertIsNone(attachments.load_ref(acc, 'offerint:1'))
            self.assertIsNotNone(attachments.load_ref(self.account, 'offer:1'))

    def send_offer(self, user, rev):
        self.client.force_login(user)
        att = {'name': 'A.pdf', 'content': b'%PDF', 'content_type': 'application/pdf'}
        with patch('offers.services.revision_attachment', return_value=att), \
                patch(SEND, return_value={'ok': True, 'message_id': '<x@y>'}):
            return self.client.post('/email/compose/', {'to': 'a@b.c', 'subject': 'Angebot', 'body': 'x',
                                                         'att_ref': [f'offer:{rev.pk}'], 'att_ref_name': ['A.pdf']})

    def test_send_without_change_permission_does_not_lock_offer(self):
        from offers.models import Offer, OfferRevision
        offer = Offer.objects.create(contact_name='Alex')
        rev = OfferRevision.objects.create(offer=offer, revision=1)
        clerk = person('clerk2', 'manager', allowed_modules=['email_assistant', 'offers'],
                       module_operations={'offers': ['view'], 'email_assistant': ['view', 'add', 'change']})
        EmailAccount.objects.create(user=clerk, email_address='clerk2@sevskiy.example', imap_username='c',
                                    imap_password='x', smtp_username='c', smtp_password='x', is_primary=True)
        self.assertTrue(self.send_offer(clerk, rev).json()['ok'])
        offer.refresh_from_db()
        rev.refresh_from_db()
        self.assertEqual((offer.status, rev.sent_at), (Offer.DRAFT, None))   # лише перегляд — не «надіслана»
        self.assertTrue(self.send_offer(self.user, rev).json()['ok'])
        offer.refresh_from_db()
        self.assertEqual(offer.status, Offer.SENT)                           # з правом змінювати — так


# ── 14. Завдання з листа — 09:00 за місцевим часом ────────────────────────────
class TaskFromMailTests(Base):
    def test_default_is_local_nine(self):
        msg = self.message()
        r = self.client.post(f'/email/message/{msg.pk}/create-task/', '{}', content_type='application/json').json()
        self.assertTrue(r['ok'])
        from calendar_app.models import CalendarEvent
        ev = CalendarEvent.objects.get(pk=r['event_id'])
        local = timezone.localtime(ev.start_at)
        self.assertEqual((local.hour, local.minute), (9, 0))
        self.assertEqual(local.date(), timezone.localdate() + datetime.timedelta(days=1))
        self.assertTrue(r['start'].endswith('09:00'))
