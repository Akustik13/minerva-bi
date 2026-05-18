"""documents/views.py"""
import logging
from django.contrib.admin.views.decorators import staff_member_required
from django.http import FileResponse, JsonResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django.db.models import Q

logger = logging.getLogger('documents')


@staff_member_required
def generate_for_order(request, order_pk, template_pk=None):
    """POST: Генерувати документ для замовлення."""
    from documents.models import DocumentTemplate
    from documents.generators import get_order_context
    from documents.service import generate_docx
    from sales.models import SalesOrder

    order = get_object_or_404(SalesOrder, pk=order_pk)

    if template_pk:
        template = get_object_or_404(DocumentTemplate, pk=template_pk, is_active=True)
    else:
        template = (DocumentTemplate.objects
                    .filter(Q(module='sales') | Q(module='any'), is_active=True)
                    .order_by('-is_default', 'sort_order')
                    .first())
        if not template:
            return JsonResponse({
                'ok': False,
                'error': 'Немає шаблонів. Додайте в /admin/documents/documenttemplate/'
            }, status=404)

    try:
        ctx = get_order_context(order_pk)
        doc = generate_docx(
            template=template,
            context=ctx,
            source_module='sales',
            source_object_id=order_pk,
            source_repr=f'Order #{ctx.get("order_number", order_pk)}',
            user=request.user,
        )

        # Copy both .docx and .pdf to media/orders/{source}/{order_number}/
        # so they appear in "Завантажені документи" panel immediately.
        from django.conf import settings as _s
        import shutil as _sh
        from datetime import date as _date
        from pathlib import Path as _P

        source_slug  = order.source or 'manual'
        order_number = order.order_number or str(order_pk)
        dest_dir = _P(_s.MEDIA_ROOT) / 'orders' / source_slug / order_number
        dest_dir.mkdir(parents=True, exist_ok=True)

        copy_url = copy_filename = None
        if doc.docx_file:
            src_path = _P(doc.docx_file.path)
            copy_filename = src_path.name
            _sh.copy2(str(src_path), str(dest_dir / copy_filename))
            copy_url = f'{_s.MEDIA_URL}orders/{source_slug}/{order_number}/{copy_filename}'

        pdf_copy_url = pdf_copy_filename = None
        if doc.pdf_file:
            try:
                pdf_src = _P(doc.pdf_file.path)
                pdf_copy_filename = pdf_src.name
                _sh.copy2(str(pdf_src), str(dest_dir / pdf_copy_filename))
                pdf_copy_url = (
                    f'{_s.MEDIA_URL}orders/{source_slug}/{order_number}/{pdf_copy_filename}'
                )
            except Exception as e:
                logger.warning('PDF copy failed: %s', e)

        return JsonResponse({
            'ok':              True,
            'doc_id':          doc.pk,
            'status':          doc.status,
            'docx_url':        f'/documents/download/{doc.pk}/docx/',
            'has_pdf':         bool(doc.pdf_file),
            'pdf_url':         f'/documents/download/{doc.pk}/pdf/' if doc.pdf_file else None,
            'filename':        doc.docx_file.name.split('/')[-1] if doc.docx_file else '',
            'file_size':       doc.file_size_display(),
            # For local save via MinervaLocalSave
            'url':             copy_url,
            'copy_filename':   copy_filename,
            'pdf_copy_url':    pdf_copy_url,
            'pdf_copy_filename': pdf_copy_filename,
            'source_slug':     source_slug,
            'date_str':        _date.today().strftime('%Y-%m-%d'),
            'order_number':    order_number,
        })
    except Exception as e:
        logger.error('generate_for_order %s: %s', order_pk, e)
        err = str(e)
        if 'is not a Word file' in err or 'themeManager' in err or 'BadZipFile' in err:
            err = (
                'Файл шаблону не є коректним Word документом (.docx). '
                'Відкрийте шаблон у Microsoft Word і збережіть через '
                '«Зберегти як» → «Word документ (.docx)», потім завантажте знову.'
            )
        elif 'Encountered unknown tag' in err or 'TemplateSyntaxError' in err:
            err = _syntax_error_hint(err)
        elif 'is undefined' in err or 'UndefinedError' in err:
            var = err.split("'")[1] if "'" in err else err
            err = (
                f'Помилка в шаблоні: змінна «{var}» не визначена. '
                'Для таблиць: перший рядок для повтору має починатись з '
                '{%tr for item in items %}, останній — {%tr endfor %}. '
                'Або використовуй звичайний цикл поза таблицею.'
            )
        return JsonResponse({'ok': False, 'error': err}, status=500)


@staff_member_required
def list_templates(request):
    """GET: Список шаблонів для модуля, з фільтрацією по source."""
    from documents.models import DocumentTemplate
    module    = request.GET.get('module', 'sales')
    source_id = request.GET.get('source_id', '')

    qs = (DocumentTemplate.objects
          .filter(is_active=True)
          .filter(Q(module=module) | Q(module='any')))

    if source_id:
        # Показуємо шаблони прив'язані до цього source + без прив'язки
        qs = qs.filter(Q(source_id=source_id) | Q(source__isnull=True))
    else:
        qs = qs.filter(source__isnull=True)

    tpls = (qs.order_by('-is_default', 'sort_order', 'name')
              .values('pk', 'name', 'doc_type', 'language', 'description'))
    return JsonResponse({'templates': list(tpls)})


@staff_member_required
def list_documents(request):
    """GET: Список збережених документів для об'єкта."""
    from documents.models import GeneratedDocument
    module = request.GET.get('module', '')
    obj_id = request.GET.get('object_id', '')
    qs = (GeneratedDocument.objects
          .filter(source_module=module, source_object_id=obj_id, status='ready')
          .order_by('-created_at')[:20])
    docs = []
    for d in qs:
        docs.append({
            'id':       d.pk,
            'name':     d.docx_file.name.split('/')[-1] if d.docx_file else '—',
            'template': str(d.template) if d.template else '—',
            'size':     d.file_size_display(),
            'date':     d.created_at.strftime('%d.%m.%Y %H:%M'),
            'docx_url': f'/documents/download/{d.pk}/docx/',
            'has_pdf':  bool(d.pdf_file),
            'pdf_url':  f'/documents/download/{d.pk}/pdf/' if d.pdf_file else None,
        })
    return JsonResponse({'documents': docs})


@staff_member_required
def download_docx(request, doc_pk):
    from documents.models import GeneratedDocument
    doc = get_object_or_404(GeneratedDocument, pk=doc_pk)
    if not doc.docx_file:
        raise Http404
    return FileResponse(
        doc.docx_file.open('rb'),
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        filename=doc.docx_file.name.split('/')[-1],
    )


@staff_member_required
def download_pdf(request, doc_pk):
    from documents.models import GeneratedDocument
    doc = get_object_or_404(GeneratedDocument, pk=doc_pk)
    if not doc.pdf_file:
        raise Http404
    return FileResponse(
        doc.pdf_file.open('rb'),
        content_type='application/pdf',
        as_attachment=True,
        filename=doc.pdf_file.name.split('/')[-1],
    )


@staff_member_required
@require_POST
def delete_document(request, doc_pk):
    from documents.models import GeneratedDocument
    doc = get_object_or_404(GeneratedDocument, pk=doc_pk)
    try:
        # Delete copies in media/orders/ so "Завантажені документи" updates correctly
        if doc.source_module == 'sales' and doc.source_object_id:
            try:
                from sales.models import SalesOrder
                from django.conf import settings
                from pathlib import Path
                order = SalesOrder.objects.get(pk=doc.source_object_id)
                orders_dir = (Path(settings.MEDIA_ROOT) / 'orders'
                              / (order.source or 'manual') / order.order_number)
                for file_field in (doc.docx_file, doc.pdf_file):
                    if file_field:
                        fname = file_field.name.split('/')[-1]
                        copy_path = orders_dir / fname
                        if copy_path.exists():
                            copy_path.unlink()
            except Exception:
                pass

        doc.delete_files()
        doc.delete()
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})


# ── Template validation ───────────────────────────────────────────────────────

# All top-level variables available in order context
_KNOWN_TOP_VARS = {
    'order_number', 'order_date', 'order_status', 'invoice_number', 'invoice_date', 'due_date',
    'customer_name', 'customer_address', 'customer_city', 'customer_country',
    'customer_email', 'customer_phone', 'customer_vat',
    'shipper_name', 'shipper_address', 'shipper_city', 'shipper_country',
    'shipper_email', 'shipper_phone', 'vat_number', 'eori_number',
    'bank_name', 'bank_iban', 'bank_swift',
    'tracking_number', 'carrier_name', 'shipping_date',
    'currency', 'subtotal', 'vat_rate', 'vat_amount', 'total_amount', 'payment_terms',
    'total_weight', 'total_items', 'items_count',
    'customs_type', 'customs_reason', 'country_of_origin', 'declared_value', 'gross_weight',
    'items', 'generated_date', 'generated_by', 'notes', 'proforma_notes',
}

_KNOWN_ITEM_VARS = {
    'sku', 'name', 'quantity', 'unit_price', 'total_price', 'weight', 'hs_code', 'country',
}


def _suggest(var_name):
    """Return the closest known variable name, or None."""
    import difflib
    if '.' in var_name:
        root, field = var_name.split('.', 1)
        if root == 'item':
            m = difflib.get_close_matches(field, _KNOWN_ITEM_VARS, n=1, cutoff=0.55)
            return f'item.{m[0]}' if m else None
        return None
    m = difflib.get_close_matches(var_name, _KNOWN_TOP_VARS, n=1, cutoff=0.55)
    return m[0] if m else None


def _collect_undefined(template_path, context):
    """Render template with a tracking Jinja2 env; return list of undefined var names.
    Propagates TemplateSyntaxError so callers can report real syntax bugs."""
    from docxtpl import DocxTemplate
    import jinja2

    collected = []

    class _TrackUndef(jinja2.Undefined):
        def __str__(self):
            n = self._undefined_name or '?'
            if n not in collected:
                collected.append(n)
            return n

        def __iter__(self): return iter([])
        def __bool__(self):  return False
        def __len__(self):   return 0

        def __getattr__(self, name):
            if name.startswith('_'):
                raise AttributeError(name)
            parent = self._undefined_name or ''
            child  = f'{parent}.{name}' if parent else name
            if child not in collected:
                collected.append(child)
            return _TrackUndef(name=child)

    tpl = DocxTemplate(template_path)
    env = jinja2.Environment(undefined=_TrackUndef)
    try:
        tpl.render(context, jinja_env=env)
    except jinja2.exceptions.TemplateSyntaxError:
        raise  # real syntax bug — let the caller handle it
    except Exception:
        pass   # runtime errors (UndefinedError, etc.) are OK during tracking pass
    return collected


def _render_validated_doc(template_path, context):
    """Render template; undefined vars show as ⚠️ [var] highlighted red in .docx.
    Works by rendering with a custom DebugUndefined that keeps {{ var }} as literal
    text, then directly patching the ZIP XML to colour those runs red.
    Raises jinja2.TemplateSyntaxError for real syntax bugs.
    """
    from docxtpl import DocxTemplate
    import jinja2
    from io import BytesIO

    class _DebugUndef(jinja2.Undefined):
        """Keeps undefined vars as {{ var }} text so we can find and colour them."""
        def __str__(self):
            return f'{{{{ {self._undefined_name or "?"} }}}}'
        def __iter__(self): return iter([])
        def __bool__(self):  return False
        def __len__(self):   return 0
        def __getattr__(self, name):
            if name.startswith('_'):
                raise AttributeError(name)
            parent = self._undefined_name or ''
            return _DebugUndef(name=f'{parent}.{name}' if parent else name)

    tpl = DocxTemplate(template_path)
    env = jinja2.Environment(undefined=_DebugUndef)
    try:
        tpl.render(context, jinja_env=env)
    except jinja2.exceptions.TemplateSyntaxError:
        raise
    except Exception:
        pass

    buf = BytesIO()
    tpl.save(buf)
    buf.seek(0)
    return BytesIO(_redden_undefined_runs(buf.read()))


def _redden_undefined_runs(docx_bytes):
    """Patch docx ZIP: colour any XML run containing {{ text red + bold + ⚠️ prefix."""
    import zipfile, re
    from io import BytesIO

    def _patch_xml(xml):
        def fix_run(m):
            run = m.group(0)
            all_text = ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', run))
            if '{{' not in all_text:
                return run

            # Replace {{ var }} text with ⚠️ [var] inside every w:t node
            def fix_wt(wt_m):
                attr  = wt_m.group(1) or ''
                inner = wt_m.group(2)
                inner = re.sub(r'\{\{\s*(.*?)\s*\}\}', r'⚠️ [\1]', inner)
                if 'preserve' not in attr:
                    attr = ' xml:space="preserve"'
                return f'<w:t{attr}>{inner}</w:t>'

            run = re.sub(r'<w:t(\s[^>]*)?>([^<]*)</w:t>', fix_wt, run)

            # Inject red colour + bold into run properties
            if '<w:rPr>' in run:
                if '<w:color' not in run:
                    run = run.replace('<w:rPr>', '<w:rPr><w:color w:val="FF0000"/>', 1)
                if '<w:b/>' not in run and '<w:b ' not in run:
                    run = run.replace('<w:rPr>', '<w:rPr><w:b/>', 1)
            else:
                run = re.sub(r'(<w:t\b)',
                             r'<w:rPr><w:color w:val="FF0000"/><w:b/></w:rPr>\1',
                             run, count=1)
            return run

        return re.sub(r'<w:r\b[^>]*>.*?</w:r>', fix_run, xml, flags=re.DOTALL)

    inp = BytesIO(docx_bytes)
    out = BytesIO()
    with zipfile.ZipFile(inp, 'r') as zin, \
         zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith('.xml') and item.filename.startswith('word/'):
                data = _patch_xml(data.decode('utf-8')).encode('utf-8')
            zout.writestr(item, data)
    out.seek(0)
    return out.read()


def _sample_context():
    """Minimal sample context with all standard variables."""
    return {
        'order_number': 'DEMO-001', 'order_date': '01.05.2025',
        'order_status': 'received',  'invoice_number': 'INV-DEMO-001',
        'invoice_date': '01.05.2025', 'due_date': '31.05.2025',
        'customer_name': 'Test GmbH',    'customer_address': 'Teststraße 1',
        'customer_city': 'Berlin, 10115', 'customer_country': 'DE',
        'customer_email': 'test@example.com', 'customer_phone': '+49 30 1234567',
        'customer_vat': 'DE123456789',
        'shipper_name': 'Our Co. GmbH',  'shipper_address': 'Main St. 5',
        'shipper_city': '20095 Hamburg', 'shipper_country': 'DE',
        'shipper_email': 'info@co.de',   'shipper_phone': '+49 40 1234567',
        'vat_number': 'DE987654321',     'eori_number': 'DE1234567890123',
        'bank_name': 'Deutsche Bank',    'bank_iban': 'DE89370400440532013000',
        'bank_swift': 'DEUTDEDB',
        'tracking_number': '1Z999AA1',   'carrier_name': 'UPS',
        'shipping_date': '01.05.2025',   'currency': 'EUR',
        'subtotal': '100.00',  'vat_rate': '19',
        'vat_amount': '19.00', 'total_amount': '119.00',
        'payment_terms': 'Payment within 30 days',
        'total_weight': '0.500', 'total_items': '2', 'items_count': '1',
        'customs_type': 'SALE', 'customs_reason': 'Commercial goods',
        'country_of_origin': 'DE', 'declared_value': '100.00', 'gross_weight': '0.500',
        'items': [{'sku': 'SKU-001', 'name': 'Test Product', 'quantity': 2,
                   'unit_price': '50.00', 'total_price': '100.00',
                   'weight': '0.250', 'hs_code': '8536.90', 'country': 'DE'}],
        'generated_date': '01.05.2025 12:00', 'generated_by': 'Minerva BI',
        'notes': '', 'proforma_notes': '',
    }


def _syntax_error_hint(err_str):
    """Translate Jinja2 TemplateSyntaxError to a human-readable Ukrainian message."""
    if 'Encountered unknown tag' in err_str:
        try:
            tag = err_str.split("'")[1]
        except IndexError:
            tag = '?'
        return (
            f'Синтаксична помилка: невідомий тег «{{% {tag} %}}». '
            f'Можливо, замість «{{{{ {tag} }}}}» (подвійні дужки) вжито «{{% {tag} %}}» (відсоток). '
            'Для виведення значень використовуй {{ }}, для циклів — {% %}. '
            'Для таблиць: {%tr for item in items %} ... {%tr endfor %}.'
        )
    if 'Unexpected end of template' in err_str or 'unexpected end' in err_str.lower():
        return (
            'Синтаксична помилка: незакритий блок у шаблоні. '
            'Перевір що кожен {% for %} має {% endfor %}, {% if %} — {% endif %}.'
        )
    return f'Синтаксична помилка в шаблоні: {err_str}'


@staff_member_required
def check_template(request, template_pk):
    """GET: Check template for undefined vars; return JSON with issues + suggestions."""
    from documents.models import DocumentTemplate
    from documents.generators import get_order_context
    import jinja2

    template = get_object_or_404(DocumentTemplate, pk=template_pk)
    order_pk = request.GET.get('order_pk')

    try:
        ctx = get_order_context(int(order_pk)) if order_pk else _sample_context()
        issues_raw = _collect_undefined(template.template_file.path, ctx)
        issues = []
        for var in issues_raw:
            suggestion = _suggest(var)
            issues.append({
                'var':        var,
                'suggestion': suggestion,
                'label':      (f'→ правильно: {{{{{suggestion}}}}}' if suggestion and suggestion != var
                               else '— невідоме поле'),
            })
        return JsonResponse({'ok': True, 'issues': issues})
    except jinja2.exceptions.TemplateSyntaxError as e:
        return JsonResponse({'ok': False, 'syntax_error': True, 'error': _syntax_error_hint(str(e))})
    except Exception as e:
        logger.warning('check_template %s: %s', template_pk, e)
        return JsonResponse({'ok': False, 'error': str(e)})


@staff_member_required
def check_template_download(request, template_pk):
    """GET: Download template with undefined vars highlighted red."""
    from documents.models import DocumentTemplate
    from documents.generators import get_order_context
    from django.http import HttpResponse
    import jinja2

    template = get_object_or_404(DocumentTemplate, pk=template_pk)
    order_pk = request.GET.get('order_pk')

    try:
        ctx = get_order_context(int(order_pk)) if order_pk else _sample_context()
        buf = _render_validated_doc(template.template_file.path, ctx)
        safe_name = template.name.replace(' ', '_')[:30]
        resp = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        )
        resp['Content-Disposition'] = f'attachment; filename="check_{safe_name}.docx"'
        return resp
    except jinja2.exceptions.TemplateSyntaxError as e:
        return HttpResponse(
            _syntax_error_hint(str(e)),
            status=422,
            content_type='text/plain; charset=utf-8',
        )
    except Exception as e:
        logger.error('check_template_download %s: %s', template_pk, e)
        return HttpResponse(str(e), status=500)
