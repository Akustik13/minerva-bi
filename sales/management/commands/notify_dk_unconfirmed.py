"""
Надсилає email + Telegram якщо DigiKey замовлення відправлено (є трек-номер)
але не підтверджено через DigiKey Ship API протягом N годин.

Запуск: python manage.py notify_dk_unconfirmed
Cron: додати в docker-compose або crontab для запуску кожні 2-4 години.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Надіслати нагадування про непідтверджені відправлення на DigiKey"

    def handle(self, *args, **options):
        from config.models import NotificationSettings
        from sales.models import SalesOrder

        ns = NotificationSettings.get()

        if not ns.dk_unconfirmed_alert_enabled:
            self.stdout.write("dk_unconfirmed_alert вимкнено — пропускаємо.")
            return

        if not ns.dk_unconfirmed_alert_email and not ns.dk_unconfirmed_alert_telegram:
            self.stdout.write("Email і Telegram вимкнені — пропускаємо.")
            return

        # Анти-спам: не надсилати частіше ніж кожні N годин
        if ns.dk_unconfirmed_last_sent:
            elapsed = (timezone.now() - ns.dk_unconfirmed_last_sent).total_seconds() / 3600
            if elapsed < ns.dk_unconfirmed_alert_hours:
                self.stdout.write(
                    f"Останнє нагадування {elapsed:.1f} год тому, поріг {ns.dk_unconfirmed_alert_hours} год — пропускаємо."
                )
                return

        threshold = timezone.now() - timezone.timedelta(hours=ns.dk_unconfirmed_alert_hours)

        orders = SalesOrder.objects.filter(
            source="digikey",
            status__in=("processing", "shipped"),
            shipped_at__isnull=False,
            shipped_at__lte=threshold,
        ).exclude(
            tracking_number=""
        ).exclude(
            status_source="DigiKey Marketplace"
        ).order_by("shipped_at")

        if not orders.exists():
            self.stdout.write("Непідтверджених відправлень не знайдено.")
            return

        lines = []
        for o in orders:
            hours_since = (timezone.now().date() - o.shipped_at).days * 24
            lines.append(
                f"• {o.order_number} | трек: {o.tracking_number} | "
                f"відправлено: {o.shipped_at} ({hours_since}+ год тому)"
            )
            self.stdout.write(f"  {lines[-1]}")

        body = (
            f"⚠️ {len(lines)} замовлень відправлено, але НЕ підтверджено на DigiKey Marketplace:\n\n"
            + "\n".join(lines)
            + f"\n\nПідтвердіть відправлення у Minerva BI → замовлення → 📤 Confirm Shipment on DigiKey."
        )

        sent_any = False

        if ns.dk_unconfirmed_alert_email and ns.email_enabled:
            try:
                from dashboard.notifications import _send_email
                _send_email(
                    ns,
                    subject=f"⚠️ {len(lines)} замовлень не підтверджено на DigiKey",
                    html_body=body.replace("\n", "<br>"),
                )
                self.stdout.write(self.style.SUCCESS("Email надіслано."))
                sent_any = True
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Email помилка: {e}"))

        if ns.dk_unconfirmed_alert_telegram and ns.telegram_enabled:
            try:
                from dashboard.notifications import _send_telegram
                _send_telegram(ns, body)
                self.stdout.write(self.style.SUCCESS("Telegram надіслано."))
                sent_any = True
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Telegram помилка: {e}"))

        if sent_any:
            ns.dk_unconfirmed_last_sent = timezone.now()
            ns.save(update_fields=["dk_unconfirmed_last_sent"])
