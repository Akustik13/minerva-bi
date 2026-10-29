from django.db import models


class LabelText(models.Model):
    """Тексти етикетки антени для артикула, які людина підтвердила у вікні перевірки (labels_app/antenna.py).

    Ключ — артикул на етикетці (з рядка замовлення): Supplier SKU DigiKey може відрізнятись від артикула товару.
    Поки запису немає — тексти беруться автоматично: склад → DigiKey → старий .dymo файл.
    `seen` — що давали джерела під час перевірки: змінилось → вікно перевірки знову."""

    sku = models.CharField("Артикул на етикетці", max_length=128, unique=True)
    product = models.ForeignKey(
        "inventory.Product", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="label_texts", verbose_name="Товар",
    )
    description = models.CharField("Опис", max_length=200, blank=True, default="")
    frequency = models.CharField("Частота", max_length=120, blank=True, default="")
    connector = models.CharField("Роз'єм", max_length=120, blank=True, default="")
    digikey_pn = models.CharField("Digi-Key P/N", max_length=80, blank=True, default="")
    seen = models.JSONField("Автоматичні значення під час перевірки", default=dict, blank=True)
    checked_at = models.DateTimeField("Перевірено", null=True, blank=True)
    checked_by = models.CharField("Хто перевірив", max_length=150, blank=True, default="")

    class Meta:
        verbose_name = "Тексти етикетки"
        verbose_name_plural = "Тексти етикеток"

    def __str__(self):
        return f"{self.sku}: {self.description}"
