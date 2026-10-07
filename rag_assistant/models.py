"""Помічник по продукції і складу: підключення до зовнішньої RAG-системи (даташити + каталог Minerva).

Окремо від «Minerva AI» (ai_assistant): власні налаштування, розмови й історія.
Ключ RAG API зберігається лише на сервері Minerva; браузер його не отримує.
"""
from django.conf import settings
from django.db import models


class RagSettings(models.Model):
    """Підключення до RAG API — синглтон (pk=1)."""
    LANGUAGES = [("uk", "Українська"), ("de", "Deutsch"), ("en", "English")]

    enabled = models.BooleanField("Увімкнено", default=False,
                                  help_text="Показувати кнопку помічника в Minerva.")
    name = models.CharField("Назва помічника", max_length=40, default="Minerva")
    base_url = models.URLField("Адреса RAG API", default="https://app.sevskiy.com",
                               help_text="Без /v1 у кінці, напр. https://app.sevskiy.com або http://192.168.2.68:8787")
    api_key = models.CharField("Ключ RAG API", max_length=200, blank=True, default="",
                               help_text="Bearer-ключ RAG («Копіювати ключ API» у RAG Documents).")
    verify_tls = models.BooleanField("Перевіряти сертифікат HTTPS", default=True)
    timeout = models.PositiveSmallIntegerField("Тайм-аут запиту, с", default=20)
    language = models.CharField("Мова відповідей", max_length=2, choices=LANGUAGES, default="uk")
    latest_only = models.BooleanField("Лише останні ревізії даташитів", default=True)
    last_check_at = models.DateTimeField("Остання перевірка", null=True, blank=True)
    last_check_result = models.CharField("Результат перевірки", max_length=255, blank=True, default="")

    class Meta:
        verbose_name = "Налаштування помічника"
        verbose_name_plural = "⚙️ Налаштування помічника"

    def __str__(self):
        return "Налаштування помічника по продукції"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


class RagConversation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="rag_conversations",
                             verbose_name="Користувач")
    title = models.CharField("Тема", max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Розмова"
        verbose_name_plural = "💬 Розмови"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"Розмова #{self.pk}"


class RagMessage(models.Model):
    ROLE_USER, ROLE_ASSISTANT = "user", "assistant"
    PENDING, DONE, FAILED = "pending", "done", "failed"

    conversation = models.ForeignKey(RagConversation, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=[(ROLE_USER, "Користувач"), (ROLE_ASSISTANT, "Помічник")])
    content = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, default=DONE,
                              choices=[(PENDING, "Виконується"), (DONE, "Готово"), (FAILED, "Помилка")])
    rag_job_id = models.CharField(max_length=64, blank=True, default="")
    stage = models.CharField(max_length=200, blank=True, default="")
    sources = models.JSONField(default=list, blank=True)
    products = models.JSONField(default=list, blank=True)
    trace = models.JSONField(default=list, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Повідомлення"
        verbose_name_plural = "Повідомлення"
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.get_role_display()}: {self.content[:60]}"
