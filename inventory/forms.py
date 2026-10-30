from django import forms

from inventory.models import InventoryTransaction


class SetStockForm(forms.Form):
    target_stock = forms.DecimalField(
        label="Фактичний залишок",
        max_digits=18,
        decimal_places=3,
        min_value=0,
        help_text="Введи фактичний залишок, який має бути на складі.",
    )


class InventoryTransactionForm(forms.ModelForm):
    """Рух складу вручну: знак кількості ставиться за типом (stock.signed_qty) — прихід завжди плюс,
    витрата й резерв — мінус, коригування — як введено. Рухи, які створюють сервіси, сюди не потрапляють."""

    class Meta:
        model = InventoryTransaction
        fields = "__all__"
        help_texts = {
            "qty": "Прихід і витрата — просто кількість (5): знак Minerva поставить сама. "
                   "Коригування — зі знаком: 3 додати, -2 списати.",
        }

    def clean(self):
        data = super().clean()
        qty, tx_type = data.get("qty"), data.get("tx_type")
        if qty is not None and tx_type:
            from inventory.services.stock import signed_qty
            data["qty"] = signed_qty(tx_type, qty)
            if data["qty"] == 0:
                self.add_error("qty", "Кількість не може бути 0.")
        return data


class ExcelUploadForm(forms.Form):
    excel_file = forms.FileField(
        label="Excel файл",
        help_text="Підтримується формат .xlsx",
        widget=forms.FileInput(attrs={"accept": ".xlsx"}),
    )
