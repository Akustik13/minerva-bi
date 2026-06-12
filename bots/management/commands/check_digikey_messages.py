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

        # Self-throttle: check interval from DB (like sync_digikey_orders)
        if not options["force"] and config.msg_last_checked_at and config.msg_check_interval:
            elapsed = (timezone.now() - config.msg_last_checked_at).total_seconds() / 60
            if elapsed < config.msg_check_interval:
                remaining = int(config.msg_check_interval - elapsed)
                self.stdout.write(
                    f"⏸ Ще рано ({int(elapsed)} хв тому, "
                    f"інтервал {config.msg_check_interval} хв). "
                    f"Наступна через ~{remaining} хв."
                )
                return

        if not config.marketplace_access_token and not config.marketplace_refresh_token:
            self.stdout.write(self.style.ERROR("❌ Marketplace token відсутній — потрібна OAuth авторизація"))
            return

        self.stdout.write("Отримуємо список тем повідомлень...")
        try:
            token = get_marketplace_token(config)
            list_items = get_all_topics_paginated(config, token, max_total=100)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"❌ Помилка API: {e}"))
            return

        self.stdout.write(f"Знайдено {len(list_items)} тем.")

        # Build lookup from existing cache to avoid re-fetching unchanged topics
        existing_cache = {
            str(t.get("id", "")): t
            for t in (config.msg_topics_cache or [])
            if t.get("id")
        }

        new_messages = []
        new_cache = []
        fetched_count = 0
        cached_count = 0

        for item in list_items:
            topic_id = str(item.get("id", ""))
            order_number = item.get("orderNumber", "")
            last_upd = item.get("lastUpdateDateUtc", "")
            if not topic_id:
                continue

            cached = existing_cache.get(topic_id)

            # Skip full fetch if lastUpdateDateUtc hasn't changed
            if cached and last_upd and cached.get("lastUpdateDateUtc") == last_upd:
                new_cache.append(cached)
                cached_count += 1
                continue

            # Fetch fresh full topic
            try:
                full = get_topic(config, token, topic_id)
                fetched_count += 1
            except Exception:
                if cached:
                    new_cache.append(cached)  # keep stale cache as fallback
                continue

            # Inject list-item fields not present in full topic response
            full["orderNumber"] = order_number
            if last_upd:
                full.setdefault("lastUpdateDateUtc", last_upd)
            new_cache.append(full)

            conversation = full.get("conversation", [])
            if not conversation:
                continue

            # Use message with max date (handles both API sort orders)
            last_msg = max(conversation, key=lambda m: m.get("createDateUtc") or "")
            last_msg_id = str(last_msg.get("id", ""))

            seen, _ = DigiKeyMessageSeen.objects.get_or_create(topic_id=topic_id)

            if seen.last_message_id == last_msg_id:
                # Оновлюємо seen (щоб не спамити якщо відповідь від Supplier)
                seen.last_message_id = last_msg_id
                seen.save()
                continue

            # Перевіряємо що останнє повідомлення від покупця (Customer)
            if last_msg.get("sender", "").lower() == "customer":
                customer_name = ""
                if order_number:
                    from sales.models import SalesOrder
                    ord_obj = SalesOrder.objects.filter(
                        order_number=str(order_number)
                    ).select_related("customer").first()
                    if ord_obj and ord_obj.customer:
                        customer_name = ord_obj.customer.name or ""
                new_messages.append({
                    "topic_id":      topic_id,
                    "topic_title":   full.get("topic", "—"),
                    "order_id":      str(full.get("orderId", "")),
                    "order_number":  order_number,
                    "content":       last_msg.get("content", "")[:300],
                    "sender":        last_msg.get("sender", ""),
                    "sender_name":   last_msg.get("senderName", ""),
                    "customer_name": customer_name,
                    "created_at":    last_msg.get("createDateUtc", ""),
                })

            seen.last_message_id = last_msg_id
            seen.save()

        self.stdout.write(f"  API: {fetched_count} оновлено, {cached_count} з кешу.")

        now = timezone.now()
        config.msg_last_checked_at = now
        config.msg_topics_cache = new_cache
        config.msg_cache_at = now
        config.save(update_fields=["msg_last_checked_at", "msg_topics_cache", "msg_cache_at"])

        if not new_messages:
            self.stdout.write("✅ Нових повідомлень від покупців немає.")
            return

        self.stdout.write(self.style.SUCCESS(f"🔔 Нових повідомлень: {len(new_messages)}"))

        from config.models import NotificationSettings
        notif = NotificationSettings.objects.filter(pk=1).first()

        for msg in new_messages:
            self.stdout.write(f"  Topic: {msg['topic_title']} | Order: {msg['order_number']}")
            self.stdout.write(f"  {msg['content'][:100]}")

            if notif and notif.dk_msg_notify_telegram:
                _notify_telegram(config, msg)
            if notif and notif.dk_msg_notify_email:
                _notify_email(config, msg, notif)


def _fmt_utc(dt_str: str) -> str:
    """ISO UTC → '12.06.2026 15:06'."""
    if not dt_str:
        return ""
    try:
        from django.utils.dateparse import parse_datetime
        dt = parse_datetime(dt_str)
        return dt.strftime("%d.%m.%Y %H:%M") if dt else dt_str
    except Exception:
        return dt_str


def _notify_telegram(config, msg: dict):
    """Надсилає Telegram сповіщення про нове повідомлення."""
    try:
        from dashboard.notifications import _send_telegram
        from config.models import NotificationSettings
        from django.conf import settings

        ns = NotificationSettings.objects.filter(pk=1).first()
        if not ns or not ns.telegram_enabled:
            return

        order_url = ""
        order_no = msg.get("order_number") or msg.get("order_id", "")
        if order_no:
            from sales.models import SalesOrder
            order = SalesOrder.objects.filter(order_number=str(order_no)).first()
            if order:
                base = getattr(settings, "PUBLIC_BASE_URL", config.public_base_url or "")
                order_url = f"\n🔗 {base}/admin/sales/salesorder/{order.pk}/change/"

        who = (
            msg.get("sender_name")
            or msg.get("customer_name")
            or msg.get("sender", "Customer")
        )
        ts = _fmt_utc(msg.get("created_at", ""))
        ts_line = f"\n🕒 {ts}" if ts else ""

        text = (
            f"💬 <b>Нове повідомлення DigiKey</b>\n"
            f"📋 Тема: {msg['topic_title']}\n"
            f"🛒 Замовлення: #{order_no}\n"
            f"👤 Від: {who}"
            f"{ts_line}\n\n"
            f"{msg['content']}"
            f"{order_url}"
        )
        _send_telegram(ns, text)
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

        order_no = msg.get("order_number") or msg.get("order_id", "")
        who = (
            msg.get("sender_name")
            or msg.get("customer_name")
            or msg.get("sender", "Customer")
        )
        ts = _fmt_utc(msg.get("created_at", ""))
        ts_line = f"<br><b>Час:</b> {ts}" if ts else ""

        subject = f"💬 Нове повідомлення DigiKey: {msg['topic_title']}"
        html_body = (
            f"<p>Нове повідомлення від покупця в DigiKey Marketplace.</p>"
            f"<p><b>Тема:</b> {msg['topic_title']}<br>"
            f"<b>Замовлення:</b> #{order_no}<br>"
            f"<b>Від:</b> {who}"
            f"{ts_line}</p>"
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
