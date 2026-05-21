from django.contrib import admin
from django.utils.html import format_html, mark_safe
from .models import DocumentTemplate, GeneratedDocument, TEMPLATE_VARIABLES_GUIDE


@admin.register(DocumentTemplate)
class DocumentTemplateAdmin(admin.ModelAdmin):
    list_display  = ('name', 'doc_type_badge', 'module', 'source',
                     'language', 'is_active', 'is_default', 'sort_order',
                     'download_template_link', 'check_col', 'created_at')
    list_filter   = ('doc_type', 'module', 'source', 'language', 'is_active')
    list_editable = ('is_active', 'is_default', 'sort_order')
    search_fields = ('name', 'description')
    readonly_fields = ('variables_guide_display', 'variable_test_display',
                       'created_at', 'updated_at', 'check_fix_actions')

    class Media:
        js = ('admin/js/document_template_check.js',)

    fieldsets = (
        ('📄 Основне', {
            'fields': (
                'name', 'doc_type', 'module', 'source', 'language',
                'description', 'is_active', 'is_default', 'sort_order',
            ),
        }),
        ('📁 Файл шаблону', {
            'fields': ('template_file', 'check_fix_actions'),
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
        ('🧪 Тест змінних', {
            'fields': ('variable_test_display',),
            'classes': ('collapse',),
            'description': 'Введи номер замовлення або назву клієнта — побачиш реальні значення всіх змінних',
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

    def variable_test_display(self, obj):
        if not obj.pk:
            return format_html(
                '<span style="color:var(--text-dim);font-size:12px">'
                '💾 Збережіть шаблон щоб активувати тест змінних</span>'
            )
        test_url = f'/documents/template/{obj.pk}/variable-test/'
        html = f"""
<div id="mvvt-{obj.pk}">
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <input id="mvvt-q-{obj.pk}" type="text"
      placeholder="Номер замовлення або назва клієнта"
      style="padding:6px 10px;border-radius:6px;font-size:13px;
             border:1px solid var(--border-strong);background:var(--bg-input);
             color:var(--text);min-width:260px;flex:1 1 260px"
      onkeydown="if(event.key==='Enter')mvVarTest({obj.pk},{test_url!r})">
    <button type="button"
      style="padding:6px 16px;border-radius:6px;font-size:13px;cursor:pointer;
             border:1px solid var(--border-strong);background:none;color:var(--text);
             white-space:nowrap"
      onclick="mvVarTest({obj.pk},{test_url!r})">🔍 Перевірити</button>
  </div>
  <div id="mvvt-out-{obj.pk}" style="margin-top:12px"></div>
</div>
<script>
(function(){{
  if(window._mvVarTestInit) return;
  window._mvVarTestInit = true;

  window._mvEsc = function(s) {{
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }};

  window.mvVarTest = async function(pk, url) {{
    var q = document.getElementById('mvvt-q-'+pk).value.trim();
    if(!q) return;
    var out = document.getElementById('mvvt-out-'+pk);
    out.innerHTML = '<span style="color:var(--text-dim)">⏳ Шукаємо…</span>';
    try {{
      var r = await fetch(url+'?q='+encodeURIComponent(q));
      var d = await r.json();
      if(!d.ok) {{
        out.innerHTML = '<span style="color:var(--err)">✗ '+_mvEsc(d.error)+'</span>';
        return;
      }}

      // Header
      var more = d.found > 1
        ? ' <span style="color:var(--text-dim);font-size:11px">(знайдено '+(d.found)+', показую перший)</span>'
        : '';
      var hdr = '<div style="margin-bottom:10px;font-size:12px">'
        + '<strong>📋 '+_mvEsc(d.order_number)+'</strong>'
        + ' — '+_mvEsc(d.customer_name)+more+'</div>';

      // Flat variables table — context is ordered array [{k,v}, ...]
      var rows = '';
      (d.context||[]).forEach(function(item) {{
        var k = item.k, v = item.v;
        var isNan = (v==='nan');
        rows += '<tr>'
          + '<td style="padding:2px 8px 2px 0;color:var(--text-dim);white-space:nowrap;'
          +   'font-size:11px;vertical-align:top">'
          +   '<code style="background:var(--bg-hover);padding:1px 5px;border-radius:3px">'
          +   '{{{{'+_mvEsc(k)+'}}}}</code></td>'
          + '<td style="padding:2px 0 2px 8px;font-size:12px;word-break:break-all;'
          +   'color:'+(isNan?'var(--text-dim)':'var(--text)')+'">'
          +   (isNan ? '<span style="opacity:.5">nan</span>' : _mvEsc(v))+'</td>'
          + '</tr>';
      }});
      var tbl = '<table style="border-collapse:collapse;width:100%;margin-bottom:14px">'+rows+'</table>';

      // Items sub-table — item_cols gives canonical column order
      var itemsHtml = '';
      if(d.items && d.items.length) {{
        var cols = d.item_cols || Object.keys(d.items[0]);
        var thead = '<tr>'+cols.map(function(c){{
          return '<th style="padding:3px 8px;font-size:11px;text-align:left;white-space:nowrap;'
            +'color:var(--text-dim);border-bottom:1px solid var(--border-strong);'
            +'background:var(--bg-hover)">item.'+_mvEsc(c)+'</th>';
        }}).join('')+'</tr>';
        var tbody = d.items.map(function(item,i){{
          return '<tr>'+cols.map(function(c){{
            var v = item[c]!==undefined ? item[c] : 'nan';
            var isNan = (v==='nan');
            return '<td style="padding:3px 8px;font-size:11px;white-space:nowrap;'
              +'color:'+(isNan?'var(--text-dim)':'var(--text)')+';">'
              +(isNan?'<span style="opacity:.5">nan</span>':_mvEsc(v))+'</td>';
          }}).join('')+'</tr>';
        }}).join('');
        var cnt = d.items_count;
        var word = cnt===1?'рядок':(cnt<5?'рядки':'рядків');
        itemsHtml = '<div style="margin-bottom:6px;font-size:12px">'
          +'<strong>items</strong>'
          +' <span style="color:var(--text-dim)">— '+cnt+' '+word+'</span></div>'
          +'<div style="overflow-x:auto;border-radius:4px;border:1px solid var(--border-strong)">'
          +'<table style="border-collapse:collapse;width:100%">'
          +'<thead>'+thead+'</thead><tbody>'+tbody+'</tbody></table></div>';
      }} else {{
        itemsHtml = '<div style="color:var(--text-dim);font-size:12px">items — порожньо</div>';
      }}

      out.innerHTML = hdr
        + '<div style="max-height:480px;overflow-y:auto;padding:12px 14px;'
        +   'background:var(--darkened-bg);border-radius:6px">'
        + tbl + itemsHtml + '</div>';
    }} catch(e) {{
      out.innerHTML = '<span style="color:var(--err)">✗ Помилка зв\\'язку</span>';
    }}
  }};
}})();
</script>
"""
        return mark_safe(html)
    variable_test_display.short_description = 'Тест'

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

    def check_col(self, obj):
        if not obj.template_file:
            return format_html('<span style="color:var(--text-dim);font-size:11px">—</span>')
        return format_html(
            '<button type="button" onclick="checkDocTemplate({})" '
            'style="padding:3px 10px;border-radius:5px;font-size:11px;'
            'border:1px solid var(--border-strong);background:none;'
            'color:var(--text);cursor:pointer;white-space:nowrap">🔍 Перевірити</button>'
            '<span id="dtc-result-{}" style="display:inline-block;margin-left:6px;'
            'vertical-align:middle"></span>',
            obj.pk, obj.pk,
        )
    check_col.short_description = 'Перевірка'

    def check_fix_actions(self, obj):
        if not obj.pk:
            return format_html(
                '<span style="color:var(--text-dim);font-size:12px">'
                '💾 Збережіть шаблон — після цього з\'являться кнопки перевірки</span>'
            )
        if not obj.template_file:
            return format_html(
                '<span style="color:var(--text-dim);font-size:12px">'
                '📁 Завантажте файл шаблону — після цього з\'являться кнопки перевірки</span>'
            )
        pk        = obj.pk
        check_url = f'/documents/template/{pk}/check/'
        dl_url    = f'/documents/template/{pk}/check-download/'
        fix_url   = f'/documents/template/{pk}/auto-fix/'
        # Inline JS — works without collectstatic
        js = f"""
if(!window._mvDTCDetail){{
  window._mvDTCDetail = async function(pk, cu, du, fu) {{
    var el = document.getElementById('mvdtc-' + pk);
    if (el) el.innerHTML = '<span style="color:var(--text-dim)">⏳ Перевіряємо…</span>';
    var dBtn = '<a href="' + du + '" style="padding:5px 12px;border-radius:5px;font-size:12px;' +
      'border:1px solid #ff9800;color:#ff9800;text-decoration:none;white-space:nowrap;margin-right:6px">' +
      '⬇ Перевірити і завантажити</a>';
    var fBtn = '<a href="' + fu + '" style="padding:5px 12px;border-radius:5px;font-size:12px;' +
      'border:1px solid var(--ok);color:var(--ok);text-decoration:none;white-space:nowrap">' +
      '🔧 Виправити і завантажити</a>';
    try {{
      var r = await fetch(cu), d = await r.json();
      if (!el) return;
      if (!d.ok) {{
        el.innerHTML = '<div style="color:var(--err);font-weight:600;margin-bottom:6px">' +
          (d.syntax_error ? '⚠️ Синтаксична помилка' : '✗ Помилка') + '</div>' +
          '<div style="margin-bottom:8px">' + d.error + '</div>' +
          '<div>' + dBtn + fBtn + '</div>';
        return;
      }}
      if (!d.issues || !d.issues.length) {{
        el.innerHTML = '<span style="color:var(--ok);font-weight:600">✓ Шаблон коректний</span>';
        return;
      }}
      el.innerHTML = '';
      if (d.loop_note) {{
        el.innerHTML += '<div style="margin-bottom:8px;padding:8px 12px;background:rgba(21,101,192,.1);' +
          'border-left:3px solid #1565c0;border-radius:4px;font-size:12px;color:#1565c0">' +
          '💡 ' + d.loop_note + '</div>';
      }}
      var errIssues = d.issues.filter(function(i){{return !i.is_item;}});
      var rows = d.issues.map(function(i) {{
        var clr = i.is_item ? '#1565c0' : 'var(--err)';
        var bg  = i.is_item ? 'rgba(21,101,192,.1)' : 'rgba(244,67,54,.12)';
        var fix = (i.suggestion && i.suggestion !== i.var)
          ? '<span style="color:var(--ok)"> → {{{{' + i.suggestion + '}}}}</span>'
          : '<span style="color:var(--text-dim)"> ' + i.label + '</span>';
        return '<div style="padding:2px 0"><code style="background:' + bg + ';' +
          'padding:1px 5px;border-radius:3px;color:' + clr + '">{{{{' + i.var + '}}}}</code>' + fix + '</div>';
      }}).join('');
      var cnt = errIssues.length;
      el.innerHTML += (cnt ? '<div style="color:#ff9800;font-weight:600;margin-bottom:6px">⚠️ ' +
        cnt + ' невідом' + (cnt === 1 ? 'е поле' : 'их полів') + '</div>' : '') +
        (rows ? '<div style="margin-bottom:8px">' + rows + '</div>' : '') +
        '<div>' + dBtn + fBtn + '</div>';
    }} catch(e) {{
      if (el) el.innerHTML = '<span style="color:var(--err)">✗ Помилка зв\\'язку</span>';
    }}
  }};
}}"""
        btn_style  = 'padding:6px 16px;border-radius:6px;font-size:13px;cursor:pointer;border:1px solid var(--border-strong);background:none;color:var(--text)'
        dl_style   = 'padding:6px 16px;border-radius:6px;font-size:13px;border:1px solid #ff9800;color:#ff9800;text-decoration:none;white-space:nowrap'
        fix_style  = 'padding:6px 16px;border-radius:6px;font-size:13px;border:1px solid var(--ok);color:var(--ok);text-decoration:none;white-space:nowrap'
        html = (
            f'<script>{js}</script>'
            f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">'
            f'<button type="button" style="{btn_style}"'
            f' onclick="_mvDTCDetail({pk},{check_url!r},{dl_url!r},{fix_url!r})">'
            f'🔍 Перевірити шаблон</button>'
            f'<a href="{dl_url}" style="{dl_style}">⬇ Перевірити і завантажити</a>'
            f'<a href="{fix_url}" style="{fix_style}">🔧 Виправити і завантажити</a>'
            f'</div>'
            f'<div id="mvdtc-{pk}" style="margin-top:10px;font-size:12px"></div>'
        )
        return mark_safe(html)
    check_fix_actions.short_description = 'Перевірка та виправлення'

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
