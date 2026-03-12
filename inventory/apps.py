from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inventory'
    verbose_name = 'Управління складом'

    def ready(self):
        """
        Підключаємо сигнали коли додаток готовий
        """
        # Імпортуємо сигнали для PurchaseOrder
        import inventory.signals
        
        # Налаштовуємо сигнали для SalesOrder (після завантаження всіх моделей)
        inventory.signals.setup_sales_signals()
