from django.db import models


class AIConversation(models.Model):
    CHANNEL_CHOICES = [
        ('telegram_private', 'Telegram Приват'),
        ('telegram_group',   'Telegram Група'),
        ('webchat',          'WebChat Minerva'),
    ]

    user_profile = models.ForeignKey(
        'core.UserProfile',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ai_conversations',
        verbose_name='Профіль юзера')
    channel           = models.CharField(max_length=30, choices=CHANNEL_CHOICES, verbose_name='Канал')
    telegram_chat_id  = models.CharField(max_length=50, blank=True)
    started_at        = models.DateTimeField(auto_now_add=True, verbose_name='Розпочато')
    last_message_at   = models.DateTimeField(auto_now=True, verbose_name='Остання активність')
    is_active         = models.BooleanField(default=True)
    total_input_tokens  = models.PositiveIntegerField(default=0)
    total_output_tokens = models.PositiveIntegerField(default=0)
    total_cost_usd      = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    class Meta:
        verbose_name = 'AI Розмова'
        verbose_name_plural = 'AI Розмови'
        ordering = ['-last_message_at']

    def __str__(self):
        user = self.user_profile.user.username if self.user_profile else 'анонім'
        return f'{user} [{self.channel}] {self.started_at:%d.%m.%Y}'


class AIMessage(models.Model):
    conversation = models.ForeignKey(
        AIConversation, on_delete=models.CASCADE,
        related_name='messages', verbose_name='Розмова')
    role = models.CharField(
        max_length=20,
        choices=[('user', 'User'), ('assistant', 'Assistant'), ('tool', 'Tool Result')])
    content     = models.TextField(verbose_name='Зміст')
    tool_name   = models.CharField(max_length=100, blank=True)
    tool_input  = models.JSONField(null=True, blank=True)
    tool_result = models.JSONField(null=True, blank=True)
    input_tokens  = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd      = models.DecimalField(max_digits=10, decimal_places=8, default=0)
    model_used    = models.CharField(max_length=50, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'AI Повідомлення'
        ordering = ['created_at']


class AIBudgetLog(models.Model):
    year  = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    total_requests      = models.PositiveIntegerField(default=0)
    total_input_tokens  = models.PositiveBigIntegerField(default=0)
    total_output_tokens = models.PositiveBigIntegerField(default=0)
    total_cost_usd      = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    alert_sent          = models.BooleanField(default=False)

    class Meta:
        verbose_name = 'AI Бюджет (місяць)'
        verbose_name_plural = 'AI Бюджет по місяцях'
        unique_together = [('year', 'month')]
        ordering = ['-year', '-month']

    def __str__(self):
        return f'{self.year}-{self.month:02d}: ${self.total_cost_usd}'

    @classmethod
    def current(cls):
        from django.utils import timezone
        now = timezone.now()
        obj, _ = cls.objects.get_or_create(year=now.year, month=now.month)
        return obj
