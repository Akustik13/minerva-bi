from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("config", "0028_customer_notify_cc"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationsettings",
            name="order_confirm_notify_enabled",
            field=models.BooleanField(
                default=False,
                verbose_name="Кнопка «📥 Підтвердження замовлення»",
                help_text="На сторінці замовлення з'являється кнопка для надсилання клієнту підтвердження отримання замовлення.",
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="order_confirm_notify_auto",
            field=models.BooleanField(
                default=False,
                verbose_name="Авто-відправка при створенні замовлення",
                help_text="Якщо увімкнено — лист надсилається автоматично при імпорті або створенні нового замовлення.",
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="order_confirm_notify_sources",
            field=models.CharField(
                blank=True,
                default="",
                max_length=500,
                verbose_name="Джерела (фільтр)",
                help_text="Slug-коди джерел через кому. Порожньо = всі джерела.",
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="order_confirm_notify_subject",
            field=models.CharField(
                blank=True,
                default="Your order #{order_number} has been received",
                max_length=500,
                verbose_name="Тема листа (шаблон)",
                help_text="Змінні: {order_number} {customer_name} {order_date} {items}",
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="order_confirm_notify_body",
            field=models.TextField(
                blank=True,
                default=(
                    "Dear {customer_name},\n\n"
                    "Thank you for your order #{order_number} placed on {order_date}.\n\n"
                    "We have received your order and it is currently being processed.\n\n"
                    "Items ordered:\n{items}\n\n"
                    "If you have any questions, please feel free to contact us.\n\n"
                    "Best regards,\n"
                    "Sevskiy GmbH Team"
                ),
                verbose_name="Текст листа (шаблон)",
                help_text="Змінні: {order_number} {customer_name} {order_date} {items}",
            ),
        ),
        migrations.AddField(
            model_name="notificationsettings",
            name="order_confirm_notify_cc",
            field=models.CharField(
                blank=True,
                default="",
                max_length=500,
                verbose_name="CC (копія) за замовчуванням",
                help_text="Email-адреси через кому.",
            ),
        ),
    ]
