"""
sales/forms.py — Форма для manual import Excel
"""
from django import forms


class SalesImportForm(forms.Form):
    """Форма для імпорту Sales з Excel."""
    
    excel_file = forms.FileField(
        label="Excel файл",
        help_text="Завантажте .xlsx файл з даними замовлень",
        widget=forms.FileInput(attrs={'accept': '.xlsx'})
    )
    
    sheet_name = forms.CharField(
        label="Назва аркуша",
        initial="digikey",
        help_text="Назва sheet в Excel файлі"
    )
    
    # Режим імпорту
    import_mode = forms.ChoiceField(
        label="Режим імпорту",
        choices=[
            ('create', '🆕 Тільки нові замовлення (пропустити існуючі)'),
            ('update', '🔄 Оновити існуючі + додати нові'),
            ('replace', '⚠️ ВИДАЛИТИ ВСІ і створити заново'),
        ],
        initial='update',
        widget=forms.RadioSelect
    )
    
    # Вибіркове оновлення полів
    update_fields = forms.MultipleChoiceField(
        label="Які поля оновлювати?",
        choices=[
            ('dates', '📅 Дати (Order Date, Shipping, Deadline)'),
            ('prices', '💰 Ціни (Unit Price, Total, Shipping Cost)'),
            ('shipping', '🚚 Доставка (Courier, Tracking, Region, Address)'),
            ('customer', '👤 Клієнт (Client, Email, Phone, Contact Name)'),
            ('products', '📦 Товари (SKU, QTY)'),
            ('all', '✅ ВСІ ПОЛЯ'),
        ],
        initial=['all'],
        widget=forms.CheckboxSelectMultiple,
        required=False
    )
    
    dry_run = forms.BooleanField(
        label="🧪 Тестовий режим (не зберігати в БД)",
        required=False,
        initial=False
    )


class SalesExcelUploadForm(forms.Form):
    """Форма завантаження файлу для 3-крокового імпорту."""
    excel_file = forms.FileField(
        label="Excel файл",
        help_text="Підтримується формат .xlsx",
        widget=forms.FileInput(attrs={"accept": ".xlsx"}),
    )
