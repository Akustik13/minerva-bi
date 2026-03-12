"""
bots/models.py — Моделі для управління ботами і API інтеграціями
"""
from django.db import models
from django.utils import timezone
from django.core.validators import RegexValidator


class DigiKeyConfig(models.Model):
    """
    Singleton (pk=1) — налаштування DigiKey Marketplace API.

    OAuth2 Client Credentials (2-legged):
      POST https://api.digikey.com/v1/oauth2/token
      → access_token (expires in 10 хв, кешується)

    Orders endpoint:
      GET https://api.digikey.com/orderstatus/v4/orders
    """
    # ── Credentials ───────────────────────────────────────────────────────────
    client_id     = models.CharField("Client ID",     max_length=200, blank=True, default="",
                                     help_text="DigiKey Developer Portal → My Apps → Client ID")
    client_secret = models.CharField("Client Secret", max_length=200, blank=True, default="",
                                     help_text="DigiKey Developer Portal → My Apps → Client Secret")
    account_id    = models.CharField("Account ID",    max_length=100, blank=True, default="",
                                     help_text="X-DIGIKEY-Account-Id (з діагностики API або DigiKey підтримки)")

    # ── Locale ────────────────────────────────────────────────────────────────
    locale_site     = models.CharField("Locale Site",     max_length=10, default="DE",
                                       help_text="DE / US / CA / GB / AT / CH / PL …")
    locale_language = models.CharField("Locale Language", max_length=10, default="en",
                                       help_text="en / de / fr …")
    locale_currency = models.CharField("Locale Currency", max_length=8,  default="EUR",
                                       help_text="EUR / USD / GBP …")

    # ── Синхронізація ─────────────────────────────────────────────────────────
    sync_enabled          = models.BooleanField("Синхронізація увімкнена", default=False)
    sync_interval_minutes = models.PositiveSmallIntegerField(
        "Інтервал синхронізації (хвилин)", default=30,
        help_text="Рекомендовано: 15–60 хв"
    )
    use_sandbox = models.BooleanField(
        "Sandbox режим (тестовий)", default=True,
        help_text="sandbox-api.digikey.com — для тестування без реальних замовлень"
    )
    last_synced_at = models.DateTimeField("Остання успішна синхронізація", null=True, blank=True)

    # ── OAuth token cache 2-legged (не редагувати вручну) ─────────────────────
    access_token     = models.TextField("Access Token (кеш)", blank=True, default="")
    token_expires_at = models.DateTimeField("Токен дійсний до (UTC)", null=True, blank=True)

    # ── OAuth 3-legged (Marketplace API) ──────────────────────────────────────
    marketplace_access_token  = models.TextField("Marketplace Access Token",  blank=True, default="")
    marketplace_refresh_token = models.TextField("Marketplace Refresh Token", blank=True, default="")
    marketplace_token_expires_at = models.DateTimeField(
        "Marketplace Token дійсний до", null=True, blank=True
    )

    # ── Phase 2: Webhook ──────────────────────────────────────────────────────
    webhook_enabled = models.BooleanField("Webhook увімкнений (Phase 2)", default=False)
    webhook_secret  = models.CharField(
        "Webhook Secret", max_length=200, blank=True, default="",
        help_text="HMAC-підпис вхідних webhook-запитів від DigiKey"
    )
    webhook_url_note = models.CharField(
        "Webhook URL (нотатка)", max_length=300, blank=True, default="",
        help_text="Публічний URL куди DigiKey надсилатиме POST. Приклад: https://akustik.synology.me:81/bots/digikey/webhook/"
    )

    class Meta:
        verbose_name        = "DigiKey — Конфігурація"
        verbose_name_plural = "DigiKey — Конфігурація"

    def __str__(self):
        return "DigiKey API"

    def save(self, *args, **kwargs):
        self.pk = 1
        # Якщо змінились credentials або режим sandbox — скидаємо кеш токена
        if self.pk:
            try:
                old = DigiKeyConfig.objects.get(pk=1)
                if (old.client_id != self.client_id
                        or old.client_secret != self.client_secret
                        or old.use_sandbox != self.use_sandbox):
                    self.access_token              = ""
                    self.token_expires_at          = None
                    self.marketplace_access_token  = ""
                    self.marketplace_refresh_token = ""
                    self.marketplace_token_expires_at = None
            except DigiKeyConfig.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Bot(models.Model):
    """Бот для автоматизації (парсинг, синхронізація, тощо)."""
    
    class BotType(models.TextChoices):
        DIGIKEY = "digikey", "DigiKey Parser"
        MOUSER  = "mouser",  "Mouser Parser"
        CUSTOM  = "custom",  "Custom Script"
    
    class Status(models.TextChoices):
        ACTIVE  = "active",  "Активний"
        PAUSED  = "paused",  "Призупинений"
        ERROR   = "error",   "Помилка"
        RUNNING = "running", "Виконується"
    
    # ── Основна інформація ────────────────────────────────────────────────────
    name        = models.CharField("Назва", max_length=100)
    bot_type    = models.CharField("Тип", max_length=20, choices=BotType.choices, default=BotType.DIGIKEY)
    description = models.TextField("Опис", blank=True, default="")
    is_active   = models.BooleanField("Увімкнено", default=True)
    status      = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PAUSED)
    
    # ── Credentials (зашифровані в production) ────────────────────────────────
    login       = models.CharField("Логін", max_length=200, blank=True, default="")
    password    = models.CharField("Пароль", max_length=200, blank=True, default="",
                                   help_text="⚠️ Буде зашифровано при збереженні")
    api_key     = models.CharField("API ключ", max_length=500, blank=True, default="")
    
    # ── Розклад виконання ─────────────────────────────────────────────────────
    schedule_enabled = models.BooleanField("Авто-запуск", default=False)
    schedule_cron    = models.CharField(
        "Розклад (cron)",
        max_length=100,
        blank=True,
        default="0 */6 * * *",  # Кожні 6 годин
        help_text="Формат: хвилина година день місяць день_тижня. Приклад: 0 */6 * * * (кожні 6 год)",
        validators=[
            RegexValidator(
                regex=r'^[\d\*\/\-\,\s]+$',
                message='Неправильний формат cron'
            )
        ]
    )
    schedule_interval_minutes = models.IntegerField(
        "Інтервал (хвилин)",
        null=True,
        blank=True,
        help_text="Альтернатива cron: запуск кожні N хвилин"
    )
    
    # ── Статистика виконання ──────────────────────────────────────────────────
    last_run_at     = models.DateTimeField("Останній запуск", null=True, blank=True)
    last_run_status = models.CharField("Статус останнього запуску", max_length=20, blank=True, default="")
    last_run_duration = models.IntegerField("Тривалість (сек)", null=True, blank=True)
    next_run_at     = models.DateTimeField("Наступний запуск", null=True, blank=True)
    total_runs      = models.IntegerField("Всього запусків", default=0)
    success_runs    = models.IntegerField("Успішних запусків", default=0)
    error_runs      = models.IntegerField("Помилок", default=0)
    
    # ── Метадані ──────────────────────────────────────────────────────────────
    created_at  = models.DateTimeField("Створено", auto_now_add=True)
    updated_at  = models.DateTimeField("Оновлено", auto_now=True)
    created_by  = models.ForeignKey("auth.User", on_delete=models.SET_NULL, 
                                    null=True, blank=True, verbose_name="Автор")
    
    class Meta:
        verbose_name = "Бот"
        verbose_name_plural = "Боти"
        ordering = ["-created_at"]
    
    def __str__(self):
        return f"{self.name} ({self.get_bot_type_display()})"
    
    def save(self, *args, **kwargs):
        """Шифрування паролю при збереженні (TODO в production)."""
        # TODO: Додати шифрування через cryptography.fernet
        super().save(*args, **kwargs)
    
    def can_run(self):
        """Чи можна запустити бота зараз."""
        return self.is_active and self.status != self.Status.RUNNING
    
    def calculate_next_run(self):
        """Розрахунок наступного запуску по cron/interval."""
        if not self.schedule_enabled:
            return None
        
        if self.schedule_interval_minutes:
            # Простий інтервал
            from datetime import timedelta
            base = self.last_run_at or timezone.now()
            return base + timedelta(minutes=self.schedule_interval_minutes)
        
        # TODO: Парсинг cron expression через croniter
        return None


class BotLog(models.Model):
    """Лог виконання бота."""
    
    class LogLevel(models.TextChoices):
        INFO    = "info",    "Інформація"
        SUCCESS = "success", "Успіх"
        WARNING = "warning", "Попередження"
        ERROR   = "error",   "Помилка"
    
    bot         = models.ForeignKey(Bot, on_delete=models.CASCADE, related_name="logs", verbose_name="Бот")
    started_at  = models.DateTimeField("Початок", auto_now_add=True)
    finished_at = models.DateTimeField("Кінець", null=True, blank=True)
    duration    = models.IntegerField("Тривалість (сек)", null=True, blank=True)
    level       = models.CharField("Рівень", max_length=20, choices=LogLevel.choices, default=LogLevel.INFO)
    message     = models.TextField("Повідомлення", blank=True, default="")
    details     = models.JSONField("Деталі", null=True, blank=True)
    
    # Результати виконання
    items_processed = models.IntegerField("Оброблено записів", default=0)
    items_created   = models.IntegerField("Створено", default=0)
    items_updated   = models.IntegerField("Оновлено", default=0)
    items_failed    = models.IntegerField("Помилок", default=0)
    
    class Meta:
        verbose_name = "Лог бота"
        verbose_name_plural = "Логи ботів"
        ordering = ["-started_at"]
    
    def __str__(self):
        return f"{self.bot.name} — {self.started_at.strftime('%d.%m.%Y %H:%M')}"
