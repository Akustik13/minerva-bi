from django.db import models


class LabelText(models.Model):
    """Тексти етикетки антени, які людина підтвердила у вікні перевірки (labels_app/antenna.py).

    Поки запису немає — тексти беруться автоматично: склад → DigiKey → старий .dymo файл.
    `seen` — що було на складі / DigiKey під час перевірки: змінилось → вікно перевірки знову."""

    product = models.OneToOneField(
        "inventory.Product", on_delete=models.CASCADE, related_name="label_text", verbose_name="Товар",
    )
    description = models.CharField("Опис", max_length=200, blank=True, default="")
    frequency = models.CharField("Частота", max_length=120, blank=True, default="")
    connector = models.CharField("Роз'єм", max_length=120, blank=True, default="")
    seen = models.JSONField("Автоматичні значення під час перевірки", default=dict, blank=True)
    checked_at = models.DateTimeField("Перевірено", null=True, blank=True)
    checked_by = models.CharField("Хто перевірив", max_length=150, blank=True, default="")

    class Meta:
        verbose_name = "Тексти етикетки"
        verbose_name_plural = "Тексти етикеток"

    def __str__(self):
        return f"{self.product.sku}: {self.description}"
