"""email_assistant/smtp_client.py — відправка листів через SMTP."""
import logging
import smtplib
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

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
            if attachments:
                msg = MIMEMultipart('mixed')
                alt = MIMEMultipart('alternative')
                if body_text:
                    alt.attach(MIMEText(body_text, 'plain', 'utf-8'))
                if body_html:
                    alt.attach(MIMEText(body_html, 'html', 'utf-8'))
                msg.attach(alt)
            else:
                msg = MIMEMultipart('alternative')
                if body_text:
                    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
                if body_html:
                    msg.attach(MIMEText(body_html, 'html', 'utf-8'))

            msg['From']    = self.account.from_header
            msg['To']      = ', '.join(to_emails)
            msg['Subject'] = subject
            if cc_emails:
                msg['Cc'] = ', '.join(cc_emails)

            if reply_to_message and reply_to_message.message_id:
                msg['In-Reply-To'] = reply_to_message.message_id
                msg['References']  = reply_to_message.message_id

            # Підпис
            settings = getattr(self.account.user, 'email_settings', None)
            if settings and settings.signature:
                sig_text = f'\n\n--\n{settings.signature}'
                if not attachments:
                    for part in msg.get_payload():
                        if part.get_content_type() == 'text/plain':
                            current = part.get_payload(decode=True).decode('utf-8', errors='replace')
                            part.set_payload(current + sig_text, charset='utf-8')
                            break

            for att in attachments:
                ct = att.get('content_type', 'application/octet-stream')
                maintype, subtype = ct.split('/', 1)
                part = MIMEBase(maintype, subtype)
                part.set_payload(att['content'])
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment', filename=att['name'])
                msg.attach(part)

            all_recipients = to_emails + cc_emails + bcc_emails

            if self.account.smtp_use_ssl:
                server = smtplib.SMTP_SSL(self.account.smtp_host, self.account.smtp_port)
            else:
                server = smtplib.SMTP(self.account.smtp_host, self.account.smtp_port)
                if self.account.smtp_use_tls:
                    server.starttls()

            server.login(self.account.smtp_username, self.account.smtp_password)
            server.sendmail(self.account.email_address, all_recipients, msg.as_bytes())
            server.quit()

            logger.info('Email sent: %s → %s', subject[:50], to_emails)
            return {'ok': True}

        except smtplib.SMTPAuthenticationError:
            return {'ok': False, 'error': 'Помилка автентифікації SMTP. Перевір логін і пароль.'}
        except smtplib.SMTPConnectError:
            return {'ok': False, 'error': f'Не вдалось підключитись до {self.account.smtp_host}'}
        except Exception as e:
            logger.error('SMTP send error: %s', e)
            return {'ok': False, 'error': str(e)}
