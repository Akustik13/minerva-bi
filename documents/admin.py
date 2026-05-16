from django.contrib import admin
from django.utils.html import format_html
from .models import DocumentTemplate, GeneratedDocument, TEMPLATE_VARIABLES_GUIDE


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display  = ('name', 'doc_type_badge', 'module', 'source',
                     'language', 'is_active', 'is_default', 'sort_order',
                     'download_template_link', 'created_at')
    list_filter   = ('doc_type', 'module', 'source', 'language', 'is_active')
    list_editable = ('is_active', 'is_default', 'sort_order')
    search_fields = ('name', 'description')
    readonly_fields = ('variables_guide_display', 'created_at', 'updated_at')

    fieldsets = (
        ('📄 Основне', {
            'fields': (
                'name', 'doc_type', 'module', 'source', 'language',
                'description', 'is_active', 'is_default', 'sort_order',
            ),
        }),
        ('📁 Файл шаблону', {
            'fields': ('template_file',),
            'description': (
                '<strong>Як створити шаблон:</strong><br>'
                '1. Відкрий Word і зроби документ<br>'
                '2. Де треба дані — пиши <code>{{order_number}}</code><br>'
                '3. Для рядків таблиці: обгорни рядок в '
                '<code>{% for item in items %}...{% endfor %}</code><br>'
                '4. Збережи як .docx і завантаж тут'
            ),
        }),
        ('📋 Довідник змінних', {
            'fields': ('variables_guide_display',),
            'classes': ('collapse',),
            'description': 'Розгорни щоб побачити всі доступні змінні',
        }),
    )

    def variables_guide_display(self, obj):
        return format_html(
            '<pre style="font-size:11px;line-height:1.6;'
            'background:var(--darkened-bg);padding:12px;'
            'border-radius:6px;overflow-x:auto;'
            'max-height:400px;white-space:pre">{}</pre>',
            TEMPLATE_VARIABLES_GUIDE,
        )
    variables_guide_display.short_description = 'Всі змінні'

    def doc_type_badge(self, obj):
        colors = {
            'packing_list': '#17a2b8',
            'proforma':     '#6610f2',
            'invoice':      '#28a745',
            'cn23':         '#fd7e14',
            'custom':       '#6c757d',
        }
        c = colors.get(obj.doc_type, '#333')
        return format_html(
            '<span style="padding:2px 8px;border-radius:4px;'
            'font-size:11px;background:{};color:#fff">{}</span>',
            c, obj.get_doc_type_display(),
        )
    doc_type_badge.short_description = 'Тип'

    def download_template_link(self, obj):
        if obj.template_file:
            return format_html(
                '<a href="{}" style="color:var(--link-fg);font-size:12px">⬇ Завантажити</a>',
                obj.template_file.url,
            )
        return '—'
    download_template_link.short_description = 'Файл'

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(GeneratedDocument)
class GeneratedDocumentAdmin(admin.ModelAdmin):
    list_display  = ('source_repr', 'template_name', 'status_badge',
                     'file_links', 'file_size_display',
                     'generated_by', 'created_at', 'delete_btn')
    list_filter   = ('status', 'source_module',
                     ('created_at', admin.DateFieldListFilter))
    search_fields = ('source_repr',)
    readonly_fields = [f.name for f in GeneratedDocument._meta.fields] + [
        'file_links', 'file_size_display']

    def template_name(self, obj):
        return str(obj.template) if obj.template else '—'
    template_name.short_description = 'Шаблон'

    def status_badge(self, obj):
        c = {'ready': 'var(--ok)', 'error': 'var(--err)',
             'generating': '#ffc107'}.get(obj.status, 'var(--text-dim)')
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            c, obj.get_status_display(),
        )
    status_badge.short_description = 'Статус'

    def file_links(self, obj):
        links = []
        if obj.docx_file:
            links.append(format_html(
                '<a href="/documents/download/{}/docx/" style="color:var(--link-fg)">⬇ Word</a>',
                obj.pk,
            ))
        if obj.pdf_file:
            links.append(format_html(
                '<a href="/documents/download/{}/pdf/" style="color:var(--err)">⬇ PDF</a>',
                obj.pk,
            ))
        return format_html(' &nbsp; '.join(str(l) for l in links)) if links else '—'
    file_links.short_description = 'Файли'

    def delete_btn(self, obj):
        if obj.status == 'ready':
            return format_html(
                '<button type="button" onclick="deleteDoc({})" '
                'style="padding:2px 8px;border-radius:4px;font-size:11px;'
                'background:none;border:1px solid var(--err);color:var(--err);'
                'cursor:pointer">Видалити</button>',
                obj.pk,
            )
        return '—'
    delete_btn.short_description = 'Дія'

    def has_add_permission(self, request):
        return False

    class Media:
        js = ('admin/js/delete_doc.js',)
