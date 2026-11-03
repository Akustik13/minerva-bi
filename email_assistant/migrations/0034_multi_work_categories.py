"""Кілька своїх категорій у листа й гілки (email_assistant/work_categories.py).

Стара категорія лишається основною (`work_category`, за нею час askDANTE) і переноситься в новий набір:
лист — у `EmailMessage.work_categories`, вибране вручну для гілки — у `EmailThread.work_categories`
(без «лише ці»: правила тепер додають до неї інші категорії). Решта категорій старих листів з'явиться
після «🔄 Перерахувати листи», нові листи отримують їх одразу."""
from django.db import migrations, models

BATCH = 2000


def _copy(through, owner_field, rows):
    batch = []
    for owner_id, cat_id in rows:
        batch.append(through(**{owner_field: owner_id, 'mailcategory_id': cat_id}))
        if len(batch) >= BATCH:
            through.objects.bulk_create(batch, ignore_conflicts=True)
            batch = []
    if batch:
        through.objects.bulk_create(batch, ignore_conflicts=True)


def forwards(apps, schema_editor):
    EmailMessage = apps.get_model('email_assistant', 'EmailMessage')
    EmailThread = apps.get_model('email_assistant', 'EmailThread')
    _copy(EmailMessage.work_categories.through, 'emailmessage_id',
          EmailMessage.objects.exclude(work_category=None).values_list('pk', 'work_category_id').iterator(chunk_size=BATCH))
    _copy(EmailThread.work_categories.through, 'emailthread_id',
          EmailThread.objects.exclude(work_category=None).values_list('pk', 'work_category_id').iterator(chunk_size=BATCH))


class Migration(migrations.Migration):

    dependencies = [
        ('email_assistant', '0033_attachments_to_docs'),
    ]

    operations = [
        migrations.AlterField(
            model_name='emailmessage',
            name='work_category',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='messages',
                to='email_assistant.mailcategory', verbose_name='Основна своя категорія',
                help_text='Основна з категорій листа: вибрана вручну для гілки або перша за порядком; за нею '
                          'рахується час askDANTE (email_assistant/work_categories.py)'),
        ),
        migrations.AddField(
            model_name='emailmessage',
            name='work_categories',
            field=models.ManyToManyField(
                blank=True, related_name='all_messages', to='email_assistant.mailcategory',
                verbose_name='Усі свої категорії',
                help_text='DigiKey, митний брокер, Інтернет-магазин… — усі категорії листа разом з основною: за '
                          'правилами й вручну (email_assistant/work_categories.py)'),
        ),
        migrations.AlterField(
            model_name='emailthread',
            name='work_category',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name='threads',
                to='email_assistant.mailcategory', verbose_name='Основна своя категорія гілки',
                help_text='Вибрано вручну основною для всієї гілки — має пріоритет над порядком категорій; за нею '
                          'рахується час askDANTE (work_categories.py)'),
        ),
        migrations.AddField(
            model_name='emailthread',
            name='work_categories',
            field=models.ManyToManyField(
                blank=True, related_name='manual_threads', to='email_assistant.mailcategory',
                verbose_name='Свої категорії гілки (вручну)',
                help_text='Додано вручну для всієї гілки — разом із категоріями за правилами (work_categories.py)'),
        ),
        migrations.AddField(
            model_name='emailthread',
            name='work_categories_only',
            field=models.BooleanField(
                default=False, verbose_name='Лише вибрані вручну',
                help_text='«Лише ця» або категорію прибрали з гілки: правила не додають гілці інших категорій'),
        ),
        migrations.AlterField(
            model_name='mailcategory',
            name='order',
            field=models.PositiveSmallIntegerField(
                default=0, verbose_name='Порядок',
                help_text='Лист, що підходить до кількох, — у кожній; основна (за нею час askDANTE) — перша за '
                          'порядком.'),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
