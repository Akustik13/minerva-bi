from django import forms


class SetStockForm(forms.Form):
    target_stock = forms.DecimalField(
        label="Target stock",
        max_digits=18,
        decimal_places=3,
        min_value=0,
        help_text="Введи фактичний залишок, який має бути на складі.",
    )


class ExcelUploadForm(forms.Form):
    excel_file = forms.FileField(
        label="Excel файл",
        help_text="Підтримується формат .xlsx",
        widget=forms.FileInput(attrs={"accept": ".xlsx"}),
    )
