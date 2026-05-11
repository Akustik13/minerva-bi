"""email_assistant/smtp_client.py — відправка листів через SMTP."""
import email.utils
import logging
import smtplib
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY

logger = logging.getLogger('email_assistant')


class SMTPClient:
    def __init__(self, account):
        self.account = account

    def send(self, to_emails: list, subject: str, body_text: str,
             body_html: str = '', cc_emails: list = None,
             bcc_emails: list = None, reply_to_message=None,
             attachments: list = None) -> dict:
        cc_emails   = cc_emails   or []
        bcc_emails  = bcc_emails  or []
        attachments = attachments or []

        try:
            msg = EmailMessage()

            # formataddr() handles non-ASCII display names via RFC 2047
            msg['From'] = email.utils.formataddr(
                (self.account.display_name or '', self.account.email_address)
            )
            msg['To']      = ', '.join(to_emails)
            msg['Subject'] = subject
            if cc_emails:
                msg['Cc'] = ', '.join(cc_emails)

            if reply_to_message and reply_to_message.message_id:
                msg['In-Reply-To'] = reply_to_message.message_id
                msg['References']  = reply_to_message.message_id

            # Append signature before setting content
            settings = getattr(self.account.user, 'email_settings', None)
            if settings and settings.signature:
                body_text = f'{body_text}\n\n--\n{settings.signature}'

            # set_content() + add_alternative() use proper CTE so no line exceeds 998 chars
            msg.set_content(body_text, charset='utf-8')
            if body_html:
                msg.add_alternative(body_html, subtype='html', charset='utf-8')

            for att in attachments:
                ct = att.get('content_type', 'application/octet-stream')
                maintype, subtype = ct.split('/', 1)
                msg.add_attachment(
                    att['content'],
                    maintype=maintype,
                    subtype=subtype,
                    filename=att['name'],
                )

            all_recipients = to_emails + cc_emails + bcc_emails

            if self.account.smtp_use_ssl:
                conn = smtplib.SMTP_SSL(self.account.smtp_host, self.account.smtp_port)
            else:
                conn = smtplib.SMTP(self.account.smtp_host, self.account.smtp_port)
                if self.account.smtp_use_tls:
                    conn.starttls()

            conn.login(self.account.smtp_username, self.account.smtp_password)
            # SMTP_POLICY ensures max_line_length=998 and proper folding/CTE
            conn.sendmail(
                self.account.email_address,
                all_recipients,
                msg.as_bytes(policy=SMTP_POLICY),
            )
            conn.quit()

            logger.info('Email sent: %s → %s', subject[:50], to_emails)
            return {'ok': True}

        except smtplib.SMTPAuthenticationError:
            return {'ok': False, 'error': 'Помилка автентифікації SMTP. Перевір логін і пароль.'}
        except smtplib.SMTPConnectError:
            return {'ok': False, 'error': f'Не вдалось підключитись до {self.account.smtp_host}'}
        except Exception as e:
            logger.error('SMTP send error: %s', e)
            return {'ok': False, 'error': str(e)}
