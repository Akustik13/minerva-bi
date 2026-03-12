import secrets
from django.db import models

SCOPE_CHOICES = [
    ('orders:read',     'Замовлення — читання'),
    ('orders:write',    'Замовлення — запис'),
    ('products:read',   'Товари — читання'),
    ('products:write',  'Товари — запис'),
    ('customers:read',  'Клієнти — читання'),
    ('customers:write', 'Клієнти — запис'),
]


class APIKey(models.Model):
    name       = models.CharField('Назва', max_length=100)
    key        = models.CharField('Ключ', max_length=64, unique=True, editable=False)
    scopes     = models.JSONField('Права доступу', default=list)
    is_active  = models.BooleanField('Активний', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used  = models.DateTimeField('Останнє використання', null=True, blank=True)
    expires_at = models.DateField('Дійсний до', null=True, blank=True)

    class Meta:
        verbose_name = 'API Ключ'
        verbose_name_plural = 'API Ключі'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = secrets.token_hex(32)  # 64 hex chars
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    def has_scope(self, scope):
        return scope in self.scopes

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False
