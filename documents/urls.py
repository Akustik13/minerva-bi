from django.urls import path
from . import views

app_name = 'documents'

urlpatterns = [
    path('order/<int:order_pk>/generate/',
         views.generate_for_order, name='gen_order'),
    path('order/<int:order_pk>/generate/<int:template_pk>/',
         views.generate_for_order, name='gen_order_tpl'),

    path('download/<int:doc_pk>/docx/',
         views.download_docx, name='download_docx'),
    path('download/<int:doc_pk>/pdf/',
         views.download_pdf,  name='download_pdf'),

    path('delete/<int:doc_pk>/',
         views.delete_document, name='delete'),

    path('templates/',
         views.list_templates, name='list_templates'),
    path('list/',
         views.list_documents, name='list_documents'),

    path('template/<int:template_pk>/check/',
         views.check_template, name='check_template'),
    path('template/<int:template_pk>/check-download/',
         views.check_template_download, name='check_template_download'),
    path('template/<int:template_pk>/auto-fix/',
         views.auto_fix_download, name='auto_fix_download'),
]
