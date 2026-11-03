from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0032_drafts_scheduled_attachments'),
    ]

    operations = [
        migrations.AddField(
            model_name='emailsettings',
            name='attachments_to_docs',
            field=models.CharField(
                choices=[('off', 'Не зберігати'), ('orders', 'У документи замовлення'),
                         ('all', 'У документи замовлення й проєкту')],
                default='off', max_length=8, verbose_name='📎 Вкладення листів → документи',
                help_text='Лист прив’язано до замовлення (номер у темі, тексті чи вкладенні) — PDF, Word, Excel, CSV… '
                          'з нього Minerva сама кладе в «Документи замовлення»; «… й проєкту» — ще й у «📎 Документи» '
                          'проєкту, до якого підтверджено гілку. Той самий файл уже є — не зберігається '
                          '(email_assistant/attachment_docs.py)'),
        ),
    ]
