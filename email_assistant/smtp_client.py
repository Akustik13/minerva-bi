"""email_assistant/smtp_client.py — відправка листів через SMTP."""
import email.utils
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY

logger = logging.getLogger('email_assistant')


SSL_MISMATCH_HINT = ('Порт SMTP і режим шифрування не збігаються: 587 — TLS (STARTTLS) без SSL, '
                     '465 — SSL без TLS. Виправте в налаштуваннях email-акаунта.')


class SMTPClient:
    """Відправка через SMTP email-акаунта скриньки (EmailAccount) — тих самих налаштувань, що й IMAP."""

    def __init__(self, account):
        self.account = account
        self.mode = ''          # SSL / STARTTLS / без шифрування — яким режимом реально підключились
        self.mode_fixed = False # True — режим із налаштувань не підійшов до порту, використано інший

    def _open(self):
        """
        SMTP-з'єднання за налаштуваннями акаунта. Якщо SSL увімкнено для порту STARTTLS (587/25)
        або навпаки — сервер відповідає «WRONG_VERSION_NUMBER»; тоді пробуємо інший режим.
        """
        host, port = self.account.smtp_host, int(self.account.smtp_port or 587)
        encrypted = bool(self.account.smtp_use_ssl or self.account.smtp_use_tls)

        def connect(implicit_tls):
            if implicit_tls:
                conn = smtplib.SMTP_SSL(host, port, timeout=30)
                self.mode = 'SSL'
                return conn
            conn = smtplib.SMTP(host, port, timeout=30)
            if encrypted:
                conn.starttls()
            self.mode = 'STARTTLS' if encrypted else 'без шифрування'
            return conn

        implicit = bool(self.account.smtp_use_ssl)
        try:
            return connect(implicit)
        except ssl.SSLError as e:
            if 'WRONG_VERSION_NUMBER' not in str(e).upper():
                raise
            logger.warning('SMTP %s:%s: SSL/TLS не відповідає порту (%s) — пробую %s',
                           host, port, e, 'STARTTLS' if implicit else 'SSL')
            conn = connect(not implicit)
            self.mode_fixed = True
            return conn

    def via(self) -> str:
        """Через що пішов лист: «viacheslav.pryimak@sevskiy.de · smtp.ionos.de:587 · STARTTLS»."""
        text = f'{self.account.email_address} · {self.account.smtp_host}:{self.account.smtp_port} · {self.mode}'
        if self.mode_fixed:
            text += (' (у налаштуваннях акаунта для цього порту вказано інший режим — '
                     'Minerva виправила автоматично; варто виправити в налаштуваннях)')
        return text

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
            # Власний Message-ID: копія в «Надісланих» і запис у Minerva — один і той самий лист
            message_id = email.utils.make_msgid(
                domain=self.account.email_address.rpartition('@')[2] or None)
            msg['Message-ID'] = message_id
            if cc_emails:
                msg['Cc'] = ', '.join(cc_emails)

            if reply_to_message and reply_to_message.message_id:
                # Відповідь, а не новий лист: поштові програми клієнта ставлять її в ту саму гілку
                msg['In-Reply-To'] = reply_to_message.message_id
                chain = [reply_to_message.in_reply_to, reply_to_message.message_id]
                msg['References']  = ' '.join(r for r in chain if r)

            # Account signature > UserProfile.smtp_signature ({name} placeholder)
            # Appended to plain-text body only (HTML body already contains sig from frontend)
            try:
                sig_html = (self.account.signature or '').strip()
                if not sig_html:
                    sig_html = (self.account.user.profile.smtp_signature or '').strip()
                if sig_html:
                    name = (self.account.user.get_full_name()
                            or self.account.display_name
                            or self.account.user.username)
                    sig_html = sig_html.replace('{name}', name)
                    # Strip HTML tags for plain-text version
                    import re as _re, html as _html
                    sig_plain = _re.sub(r'<br\s*/?>', '\n', sig_html, flags=_re.IGNORECASE)
                    sig_plain = _re.sub(r'<p[^>]*>', '\n', sig_plain, flags=_re.IGNORECASE)
                    sig_plain = _re.sub(r'<[^>]+>', '', sig_plain)
                    sig_plain = _html.unescape(sig_plain).strip()
                    sig_pos = getattr(self.account, 'signature_position', 'after_reply')
                    if sig_pos == 'after_reply':
                        # Insert before quoted original message (if any)
                        quote_markers = ['\n--- Оригінальний лист ---', '\n--- Переслано ---']
                        insert_at = None
                        for marker in quote_markers:
                            idx = body_text.find(marker)
                            if idx != -1:
                                insert_at = idx
                                break
                        if insert_at is not None:
                            body_text = (body_text[:insert_at]
                                         + f'\n\n--\n{sig_plain}'
                                         + body_text[insert_at:])
                        else:
                            body_text = f'{body_text}\n\n--\n{sig_plain}'
                    else:
                        # 'end': append after everything
                        body_text = f'{body_text}\n\n--\n{sig_plain}'
            except Exception:
                pass

            # set_content() + add_alternative() use proper CTE → no line > 998 chars
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
            msg_bytes = msg.as_bytes(policy=SMTP_POLICY)

            conn = self._open()

            conn.login(self.account.smtp_username, self.account.smtp_password)
            conn.sendmail(self.account.email_address, all_recipients, msg_bytes)
            conn.quit()

            # Save copy to IMAP Sent folder
            try:
                from email_assistant.imap_client import IMAPClient
                with IMAPClient(self.account) as imap:
                    imap.append_to_sent(msg_bytes)
            except Exception as e:
                logger.warning('Could not save to IMAP sent: %s', e)

            logger.info('Email sent: %s → %s via %s', subject[:50], to_emails, self.via())
            return {'ok': True, 'message_id': message_id, 'via': self.via(),
                    'mode_fixed': self.mode_fixed}

        except smtplib.SMTPAuthenticationError:
            return {'ok': False, 'error': 'Помилка автентифікації SMTP. Перевір логін і пароль.'}
        except ssl.SSLError as e:
            logger.error('SMTP SSL error: %s', e)
            return {'ok': False, 'error': f'SMTP SSL/TLS: {e}. {SSL_MISMATCH_HINT}'}
        except smtplib.SMTPConnectError:
            return {'ok': False, 'error': f'Не вдалось підключитись до {self.account.smtp_host}'}
        except Exception as e:
            logger.error('SMTP send error: %s', e)
            return {'ok': False, 'error': str(e)}
