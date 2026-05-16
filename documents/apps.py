from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name         = 'documents'
    verbose_name = 'Документи'

    def ready(self):
        pass
