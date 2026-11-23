from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('agent', '0021_mcp_kinds'),
    ]

    operations = [
        migrations.AlterField(
            model_name='agentproposal',
            name='kind',
            field=models.CharField(choices=[('field', 'Поле товару'), ('attribute', 'Технічний параметр'), ('mail', 'Лист'), ('stock_in', '📦 Прихід на склад'), ('product', '🆕 Новий товар'), ('event', '📅 Подія в календарі'), ('shop', '🏪 Інтернет-магазин'), ('dk_prices', '💲 Ціни на DigiKey'), ('dk_lead', '⏱ Строк на DigiKey'), ('shipment', '🚚 Чернетка відправлення'), ('order', '🧾 Зміна замовлення'), ('purchase', '🛒 Закупівля'), ('parcel', '📥 Посилка до нас'), ('offer', '📝 Чернетка Angebot'), ('crm_note', '👤 Нотатка клієнту CRM'), ('moment', '📌 Момент проєкту'), ('worktime', '⏱ Час в askDANTE'), ('dk_reply', '💬 Відповідь покупцю DigiKey'), ('packaging', '📦 Коробка'), ('pack_merge', '📦 Злити коробки')], max_length=10, verbose_name='Що'),
        ),
    ]
