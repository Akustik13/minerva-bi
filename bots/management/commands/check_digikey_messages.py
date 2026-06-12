"""
python manage.py check_digikey_messages

Перевіряє нові повідомлення DigiKey Marketplace і надсилає сповіщення.
Запускати через cron або вручну.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Check DigiKey Marketplace for new buyer messages and notify"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Запустити навіть якщо msg_check_enabled=False",
        )

    def handle(self, *args, **options):
        from bots.models import DigiKeyConfig, DigiKeyMessageSeen
        from bots.services.digikey import get_marketplace_token
        from bots.services.digikey_messages import get_all_topics_paginated, get_topic

        config = DigiKeyConfig.get()

        if not config.msg_check_enabled and not options["force"]:
            self.stdout.write("msg_check_enabled=False — пропускаємо. Використай --force щоб запустити.")
            return

        if not config.marketplace_access_token and not config.marketplace_refresh_token:
            self.stdout.write(self.style.ERROR("❌ Marketplace token відсутній — потрібна OAuth авторизація"))
            return

        self.stdout.write("Отримуємо список тем повідомлень...")
        try:
            token = get_marketplace_token(config)
            topics = get_all_topics_paginated(config, token, max_total=100)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Помилка API: {e}"))
            return

        self.stdout.write(f"Знайдено {len(topics)} тем.")
        new_messages = []

        for topic in topics:
            topic_id = str(topic.get("id", ""))
            if not topic_id:
                continue

            # Отримуємо повну розмову
            try:
                full = get_topic(config, token, topic_id)
            except Exception:
                continue

            conversation = full.get("conversation", [])
            if not conversation:
                continue

            last_msg = conversation[-1]
            last_msg_id = str(last_msg.get("id", ""))

            seen, _ = DigiKeyMessageSeen.objects.get_or_create(topic_id=topic_id)

            if seen.last_message_id == last_msg_id:
                continue  # нічого нового

            # Перевіряємо що останнє повідомлення від покупця (Customer)
            if last_msg.get("sender", "").lower() == "customer":
                new_messages.append({
                    "topic_id":    topic_id,
                    "topic_title": full.get("topic", "—"),
                    "order_id":    str(full.get("orderId", "")),
                    "content":     last_msg.get("content", "")[:300],
                    "sender":      last_msg.get("sender", ""),
                    "created_at":  last_msg.get("createDateUtc", ""),
                })

            # Оновлюємо seen незалежно від sender (щоб не спамити)
            seen.last_message_id = last_msg_id
            seen.save()

        config.msg_last_checked_at = timezone.now()
        config.save(update_fields=["msg_last_checked_at"])

        if not new_messages:
            self.stdout.write("✅ Нових повідомлень від покупців немає.")
            return

        self.stdout.write(self.style.SUCCESS(f"🔔 Нових повідомлень: {len(new_messages)}"))

        from config.models import NotificationSettings
        notif = NotificationSettings.objects.filter(pk=1).first()

        for msg in new_messages:
            self.stdout.write(f"  Topic: {msg['topic_title']} | Order: {msg['order_id']}")
            self.stdout.write(f"  {msg['content'][:100]}")

            if notif and notif.dk_msg_notify_telegram:
                _notify_telegram(config, msg)
            if notif and notif.dk_msg_notify_email:
                _notify_email(config, msg, notif)


def _notify_telegram(config, msg: dict):
    """Надсилає Telegram сповіщення про нове повідомлення."""
    try:
        from dashboard.notifications import _send_telegram
        from django.conf import settings

        order_url = ""
        # Спробуємо знайти замовлення за order_id (DigiKey order number)
        order_no = msg.get("order_id", "")
        if order_no:
            from sales.models import SalesOrder
            order = SalesOrder.objects.filter(order_number=order_no).first()
            if order:
                base = getattr(settings, "PUBLIC_BASE_URL", config.public_base_url or "")
                order_url = f"\n🔗 {base}/admin/sales/salesorder/{order.pk}/change/"

        text = (
            f"💬 <b>Нове повідомлення DigiKey</b>\n"
            f"📋 Тема: {msg['topic_title']}\n"
            f"🛒 Замовлення: {msg['order_id']}\n"
            f"👤 Від: {msg['sender']}\n\n"
            f"{msg['content']}"
            f"{order_url}"
        )
        _send_telegram(text)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Telegram notify failed: %s", e)


def _notify_email(config, msg: dict, notif=None):
    """Надсилає Email сповіщення про нове повідомлення."""
    try:
        from dashboard.notifications import _send_event_email
        from config.models import NotificationSettings

        ns = notif or NotificationSettings.objects.filter(pk=1).first()
        if not ns or not ns.email_enabled:
            return

        subject = f"💬 Нове повідомлення DigiKey: {msg['topic_title']}"
        html_body = (
            f"<p>Нове повідомлення від покупця в DigiKey Marketplace.</p>"
            f"<p><b>Тема:</b> {msg['topic_title']}<br>"
            f"<b>Замовлення:</b> {msg['order_id']}<br>"
            f"<b>Від:</b> {msg['sender']}</p>"
            f"<p><b>Повідомлення:</b><br>{msg['content']}</p>"
        )

        # Override recipients if DK-specific list is set
        dk_to = (ns.dk_msg_notify_email_to or '').strip()
        if dk_to:
            import copy
            ns_copy = copy.copy(ns)
            ns_copy.email_to = dk_to
            _send_event_email(ns_copy, subject, html_body)
        else:
            _send_event_email(ns, subject, html_body)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("Email notify failed: %s", e)
