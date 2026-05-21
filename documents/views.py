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


def _redden_syntax_tags(docx_bytes):
    """Patch docx ZIP: colour {% %} tags red+bold without rendering the template.
    Used when TemplateSyntaxError prevents normal rendering.
    Works at paragraph level: concatenates all run texts, maps character positions
    back to individual runs, then marks overlapping runs red in reverse order."""
    import zipfile, re
    from io import BytesIO

    TAG_RE = re.compile(r'\{%-?\s*\S.*?-?%\}', re.DOTALL)

    def _process_para(para_xml):
        run_matches = list(re.finditer(r'<w:r\b[^>]*>.*?</w:r>', para_xml, re.DOTALL))
        if not run_matches:
            return para_xml

        full_text = ''
        char_to_idx = []
        for idx, rm in enumerate(run_matches):
            t = ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', rm.group(0)))
            char_to_idx.extend([idx] * len(t))
            full_text += t

        if '{%' not in full_text:
            return para_xml

        red_set = set()
        for m in TAG_RE.finditer(full_text):
            for p in range(m.start(), min(m.end(), len(char_to_idx))):
                red_set.add(char_to_idx[p])

        if not red_set:
            return para_xml

        result = para_xml
        for rev_i, rm in enumerate(reversed(run_matches)):
            actual = len(run_matches) - 1 - rev_i
            if actual not in red_set:
                continue
            rx = rm.group(0)
            if '<w:rPr>' in rx:
                if '<w:color' not in rx:
                    rx = rx.replace('<w:rPr>', '<w:rPr><w:color w:val="FF0000"/>', 1)
                if '<w:b/>' not in rx and '<w:b ' not in rx:
                    rx = rx.replace('<w:rPr>', '<w:rPr><w:b/>', 1)
            else:
                rx = re.sub(r'(<w:t\b)',
                            r'<w:rPr><w:color w:val="FF0000"/><w:b/></w:rPr>\1',
                            rx, count=1)
            result = result[:rm.start()] + rx + result[rm.end():]
        return result

    def _patch_xml(xml):
        return re.sub(
            r'<w:p\b[^>]*>.*?</w:p>',
            lambda m: _process_para(m.group(0)),
            xml, flags=re.DOTALL,
        )

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


def _auto_fix_field_errors(docx_bytes, issues):
    """Patch docx: {{ unknown_var }} → {{ suggestion }} green+bold if suggestion exists,
    else → ⚠️ [var] red+bold. Run-level XML patching, no rendering needed."""
    import zipfile, re
    from io import BytesIO

    fix_map = {}
    for issue in issues:
        var = issue['var']
        sug = issue.get('suggestion') or ''
        if sug and sug != var:
            fix_map[var] = (f'{{{{ {sug} }}}}', '2E7D32')          # green — renamed
        elif var.startswith('item.') and var.split('.', 1)[1] in _KNOWN_ITEM_VARS:
            fix_map[var] = (f'{{{{ {var} }}}}', '1565C0')          # blue — valid in loop
        else:
            fix_map[var] = (f'⚠️ [{var}]', 'FF0000')               # red — truly unknown

    if not fix_map:
        return docx_bytes

    def _patch_xml(xml):
        def fix_run(m):
            run = m.group(0)
            all_text = ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', run))
            if '{{' not in all_text:
                return run
            chosen_color = [None]

            def replace_var(vm):
                var = vm.group(1).strip()
                if var not in fix_map:
                    return vm.group(0)
                repl, color = fix_map[var]
                chosen_color[0] = color
                return repl

            def fix_wt(wt_m):
                attr = wt_m.group(1) or ''
                inner = wt_m.group(2)
                new_inner = re.sub(r'\{\{\s*([\w.]+)\s*\}\}', replace_var, inner)
                if new_inner != inner and 'preserve' not in attr:
                    attr = ' xml:space="preserve"'
                return f'<w:t{attr}>{new_inner}</w:t>'

            run = re.sub(r'<w:t(\s[^>]*)?>([^<]*)</w:t>', fix_wt, run)
            c = chosen_color[0]
            if c:
                if '<w:rPr>' in run:
                    if '<w:color' not in run:
                        run = run.replace('<w:rPr>', f'<w:rPr><w:color w:val="{c}"/>', 1)
                    if '<w:b/>' not in run and '<w:b ' not in run:
                        run = run.replace('<w:rPr>', '<w:rPr><w:b/>', 1)
                else:
                    run = re.sub(r'(<w:t\b)',
                                 f'<w:rPr><w:color w:val="{c}"/><w:b/></w:rPr>\\1',
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


def _add_item_loop_markers(docx_bytes):
    """Insert dedicated {%tr for item in items %} / {%tr endfor %} rows
    around table rows that contain {{ item.xxx }} variables but lack loop markers.

    IMPORTANT: docxtpl replaces the ENTIRE <w:tr> containing a {%tr %} marker
    with the corresponding Jinja2 tag.  The markers MUST live in their own
    dedicated rows — never inside the data rows.  Data rows are left untouched
    so Jinja2 can repeat them for each item.

    Also handles the broken case where {%tr for/endfor %} were previously
    injected INTO the data row — strips those runs from the data row first.
    """
    import zipfile, re
    from io import BytesIO

    ROW_RE  = re.compile(r'<w:tr\b[^>]*>.*?</w:tr>', re.DOTALL)
    RUN_RE  = re.compile(r'<w:r\b[^>]*>.*?</w:r>', re.DOTALL)
    TEXT_RE = re.compile(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>')
    ITEM_RE = re.compile(r'\{\{\s*item\.')
    TR_RE   = re.compile(r'\{%tr\b')

    FOR_ROW = (
        '<w:tr><w:tc><w:p><w:r>'
        '<w:t xml:space="preserve">{%tr for item in items %}</w:t>'
        '</w:r></w:p></w:tc></w:tr>'
    )
    END_ROW = (
        '<w:tr><w:tc><w:p><w:r>'
        '<w:t xml:space="preserve">{%tr endfor %}</w:t>'
        '</w:r></w:p></w:tc></w:tr>'
    )

    RUN_RE  = re.compile(r'<w:r\b[^>]*>.*?</w:r>', re.DOTALL)

    def _row_text(row):
        return ''.join(TEXT_RE.findall(row))

    def _strip_tr_runs(row_xml):
        """Remove <w:r> elements whose sole text content is a {%tr %} marker."""
        # Build replacement by iterating runs and dropping marker-only ones
        result = row_xml
        # Process in reverse so positions stay valid
        for rm in reversed(list(RUN_RE.finditer(row_xml))):
            run_text = ''.join(TEXT_RE.findall(rm.group(0))).strip()
            if TR_RE.match(run_text):  # starts with {%tr
                result = result[:rm.start()] + result[rm.end():]
        return result

    def _patch_xml(xml):
        rows = list(ROW_RE.finditer(xml))

        # Phase 1: Remove {%tr %} runs that were incorrectly injected INTO data rows.
        # A "broken" row has {{ item.xxx }} AND {%tr %} markers in the same <w:tr>.
        # Process in reverse order to keep earlier offsets valid.
        for rm in reversed(rows):
            row = rm.group(0)
            text = _row_text(row)
            if ITEM_RE.search(text) and TR_RE.search(text):
                clean_row = _strip_tr_runs(row)
                if clean_row != row:
                    xml = xml[:rm.start()] + clean_row + xml[rm.end():]

        # Phase 2: Find rows with {{ item.xxx }} that still lack loop markers.
        rows = list(ROW_RE.finditer(xml))
        item_rows = [
            rm for rm in rows
            if ITEM_RE.search(_row_text(rm.group(0)))
            and not TR_RE.search(_row_text(rm.group(0)))
            and not TR_RE.search(rm.group(0))
        ]
        if not item_rows:
            return xml

        first, last = item_rows[0], item_rows[-1]
        # Insert END_ROW after last data row first (preserves earlier offsets)
        xml = xml[:last.end()] + END_ROW + xml[last.end():]
        # Insert FOR_ROW before first data row
        xml = xml[:first.start()] + FOR_ROW + xml[first.start():]
        return xml

    inp = BytesIO(docx_bytes)
    out = BytesIO()
    with zipfile.ZipFile(inp, 'r') as zin, \
         zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == 'word/document.xml':
                data = _patch_xml(data.decode('utf-8')).encode('utf-8')
            zout.writestr(info, data)
    out.seek(0)
    return out.read()


def _remove_unmatched_endtags(docx_bytes):
    """Remove stray {% endfor %} / {% endif %} from paragraph XML runs.
    When the count of end-tags exceeds the matching open-tags (e.g. the loop
    lives in {%tr for %}/{%tr endfor %} table markers, not in paragraphs),
    standalone end-tags in paragraphs are unmatched and cause TemplateSyntaxError."""
    import zipfile, re
    from io import BytesIO

    # These regexes intentionally do NOT match {%tr ... %} variants
    PARA_FOR_RE    = re.compile(r'\{%-?\s*for\b')
    PARA_ENDFOR_RE = re.compile(r'\{%-?\s*endfor\b')
    PARA_IF_RE     = re.compile(r'\{%-?\s*if\b')
    PARA_ENDIF_RE  = re.compile(r'\{%-?\s*endif\b')

    def _patch_xml(xml):
        n_for    = len(PARA_FOR_RE.findall(xml))
        n_endfor = len(PARA_ENDFOR_RE.findall(xml))
        n_if     = len(PARA_IF_RE.findall(xml))
        n_endif  = len(PARA_ENDIF_RE.findall(xml))
        remove_endfor = n_endfor > n_for
        remove_endif  = n_endif > n_if
        if not remove_endfor and not remove_endif:
            return xml

        def _fix_para(para_xml):
            text = ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', para_xml))
            if 'endfor' not in text and 'endif' not in text:
                return para_xml
            run_matches = list(re.finditer(r'<w:r\b[^>]*>.*?</w:r>', para_xml, re.DOTALL))
            remove_idx = set()
            for idx, rm in enumerate(run_matches):
                t = ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', rm.group(0)))
                if remove_endfor and PARA_ENDFOR_RE.search(t):
                    remove_idx.add(idx)
                if remove_endif and PARA_ENDIF_RE.search(t):
                    remove_idx.add(idx)
            if not remove_idx:
                return para_xml
            result = para_xml
            for rev_i, rm in enumerate(reversed(run_matches)):
                actual = len(run_matches) - 1 - rev_i
                if actual not in remove_idx:
                    continue
                result = result[:rm.start()] + result[rm.end():]
            return result

        return re.sub(r'<w:p\b[^>]*>.*?</w:p>',
                      lambda m: _fix_para(m.group(0)),
                      xml, flags=re.DOTALL)

    inp = BytesIO(docx_bytes)
    out = BytesIO()
    with zipfile.ZipFile(inp, 'r') as zin, \
         zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename.endswith('.xml') and info.filename.startswith('word/'):
                data = _patch_xml(data.decode('utf-8')).encode('utf-8')
            zout.writestr(info, data)
    out.seek(0)
    return out.read()


def _auto_fix_syntax_tags(docx_bytes):
    """Replace {% varname %} → {{ varname }} green+bold at paragraph level.
    Skips real Jinja2 block tags (for/endfor/if/etc.) and docxtpl row markers (tr/p/tc)."""
    import zipfile, re
    from io import BytesIO

    _KEEP = frozenset({
        'for', 'endfor', 'if', 'endif', 'else', 'elif',
        'set', 'block', 'endblock', 'raw', 'endraw',
        'with', 'endwith', 'macro', 'call', 'tr', 'p', 'tc',
    })
    TAG_RE = re.compile(r'\{%-?\s*([\w.]+)(?:\s[^%]*)?\s*-?%\}', re.DOTALL)

    def _process_para(para_xml):
        run_matches = list(re.finditer(r'<w:r\b[^>]*>.*?</w:r>', para_xml, re.DOTALL))
        if not run_matches:
            return para_xml
        full_text = ''
        char_to_idx = []
        for idx, rm in enumerate(run_matches):
            t = ''.join(re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', rm.group(0)))
            char_to_idx.extend([idx] * len(t))
            full_text += t
        if '{%' not in full_text:
            return para_xml

        segments = []
        last = 0
        found_any = False
        for m in TAG_RE.finditer(full_text):
            tag_name = m.group(1).split('.')[0]
            if tag_name in _KEEP:
                continue
            found_any = True
            if m.start() > last:
                segments.append((full_text[last:m.start()], False))
            segments.append((f'{{{{ {m.group(1)} }}}}', True))
            last = m.end()
        if not found_any:
            return para_xml
        if last < len(full_text):
            segments.append((full_text[last:], False))

        first_rpr_m = re.search(r'<w:rPr>.*?</w:rPr>', run_matches[0].group(0), re.DOTALL)
        base_rpr = first_rpr_m.group(0) if first_rpr_m else ''

        new_runs = ''
        for text, is_fixed in segments:
            if not text:
                continue
            esc = (text.replace('&', '&amp;')
                       .replace('<', '&lt;').replace('>', '&gt;'))
            if is_fixed:
                new_runs += (
                    f'<w:r><w:rPr><w:color w:val="2E7D32"/><w:b/></w:rPr>'
                    f'<w:t xml:space="preserve">{esc}</w:t></w:r>'
                )
            else:
                new_runs += (
                    f'<w:r>{base_rpr}'
                    f'<w:t xml:space="preserve">{esc}</w:t></w:r>'
                )

        first_start = run_matches[0].start()
        last_end = run_matches[-1].end()
        return para_xml[:first_start] + new_runs + para_xml[last_end:]

    def _patch_xml(xml):
        return re.sub(r'<w:p\b[^>]*>.*?</w:p>',
                      lambda m: _process_para(m.group(0)),
                      xml, flags=re.DOTALL)

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
        _END_TAGS = {'endfor', 'endif', 'endblock', 'endwith', 'endmacro'}
        if tag in _END_TAGS:
            open_tag = tag[3:]  # endfor → for, endif → if
            return (
                f'Синтаксична помилка: зайвий «{{% {tag} %}}» без відповідного «{{% {open_tag} ... %}}». '
                f'Якщо цикл у таблиці — використовуй {{%tr for item in items %}} ... {{%tr endfor %}} '
                f'у рядках таблиці, а не «{{% {tag} %}}» у тексті параграфу. '
                f'Видали «{{% {tag} %}}» з шаблону або натисни «🔧 Виправити і завантажити» для автовиправлення.'
            )
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
        has_item_vars = False
        for var in issues_raw:
            suggestion = _suggest(var)
            is_item = (var.startswith('item.') and
                       var.split('.', 1)[1] in _KNOWN_ITEM_VARS)
            if is_item:
                has_item_vars = True
                label = '✓ коректна змінна — тільки всередині циклу по рядку таблиці'
            elif suggestion and suggestion != var:
                label = f'→ правильно: {{{{{suggestion}}}}}'
            else:
                label = '— невідоме поле'
            issues.append({
                'var': var, 'suggestion': suggestion,
                'label': label, 'is_item': is_item,
            })
        loop_note = (
            'Змінні item.* доступні лише всередині рядка таблиці з циклом. '
            'Додай у перший рядок даних: {%tr for item in items %} '
            'та в останній: {%tr endfor %}'
        ) if has_item_vars else None
        return JsonResponse({'ok': True, 'issues': issues, 'loop_note': loop_note})
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
    except jinja2.exceptions.TemplateSyntaxError:
        # Render failed — highlight {% %} tags directly in the original file bytes
        try:
            with open(template.template_file.path, 'rb') as fh:
                patched = _redden_syntax_tags(fh.read())
            safe_name = template.name.replace(' ', '_')[:30]
            resp = HttpResponse(
                patched,
                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            )
            resp['Content-Disposition'] = f'attachment; filename="check_{safe_name}.docx"'
            return resp
        except Exception as e2:
            logger.error('check_template_download syntax fallback %s: %s', template_pk, e2)
            return HttpResponse(str(e2), status=500)
    except Exception as e:
        logger.error('check_template_download %s: %s', template_pk, e)
        return HttpResponse(str(e), status=500)


@staff_member_required
def auto_fix_download(request, template_pk):
    """GET: Download template with auto-applied fixes.
    Field errors: {{ unknown }} → {{ suggestion }} green or ⚠️ [var] red.
    Syntax errors: {% tag %} → {{ tag }} green (for non-Jinja2 tags).
    """
    from documents.models import DocumentTemplate
    from documents.generators import get_order_context
    from django.http import HttpResponse
    import jinja2

    template = get_object_or_404(DocumentTemplate, pk=template_pk)
    order_pk = request.GET.get('order_pk')
    safe_name = template.name.replace(' ', '_')[:30]

    try:
        with open(template.template_file.path, 'rb') as fh:
            original_bytes = fh.read()
    except Exception as e:
        return HttpResponse(str(e), status=500)

    try:
        ctx = get_order_context(int(order_pk)) if order_pk else _sample_context()
        issues_raw = _collect_undefined(template.template_file.path, ctx)
        issues = [
            {'var': var, 'suggestion': _suggest(var)}
            for var in issues_raw
        ]
        patched = _auto_fix_field_errors(original_bytes, issues)
    except jinja2.exceptions.TemplateSyntaxError:
        patched = _auto_fix_syntax_tags(original_bytes)
        # Remove stray {% endfor %} / {% endif %} that have no matching opener —
        # they caused the TemplateSyntaxError and _auto_fix_syntax_tags keeps them.
        try:
            patched = _remove_unmatched_endtags(patched)
        except Exception:
            pass
    except Exception as e:
        logger.error('auto_fix_download %s: %s', template_pk, e)
        return HttpResponse(str(e), status=500)

    # Always try to inject {%tr for item in items %} / {%tr endfor %}
    # into rows that have {{ item.xxx }} but no loop markers yet.
    try:
        patched = _add_item_loop_markers(patched)
    except Exception as e:
        logger.warning('auto_fix_download loop markers %s: %s', template_pk, e)

    resp = HttpResponse(
        patched,
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
    resp['Content-Disposition'] = f'attachment; filename="fixed_{safe_name}.docx"'
    return resp


@staff_member_required
def variable_test_view(request, template_pk):
    """AJAX: return real context values for a given order number or customer name."""
    from sales.models import SalesOrder
    from documents.generators import get_order_context
    from documents.models import CONTEXT_VARIABLES_ORDER, ITEM_VARIABLES_ORDER

    q = request.GET.get('q', '').strip()
    if not q:
        return JsonResponse({'ok': False, 'error': 'Введіть номер замовлення або назву клієнта'})

    qs = SalesOrder.objects.filter(
        Q(order_number__icontains=q) |
        Q(client__icontains=q) |
        Q(ship_name__icontains=q)
    ).order_by('-order_date')[:10]

    if not qs.exists():
        return JsonResponse({'ok': False, 'error': f'Замовлення не знайдено: «{q}»'})

    order = qs.first()
    try:
        ctx = get_order_context(order.pk)
    except Exception as e:
        return JsonResponse({'ok': False, 'error': f'Помилка побудови контексту: {e}'})

    def _val(v):
        """Empty/None → 'nan', otherwise string."""
        if v is None:
            return 'nan'
        s = str(v).strip()
        return s if s else 'nan'

    items_raw = ctx.pop('items', [])

    # Return flat context in canonical guide order; unknowns appended at the end
    ordered_ctx = []
    seen = set()
    for key in CONTEXT_VARIABLES_ORDER:
        ordered_ctx.append({'k': key, 'v': _val(ctx.get(key))})
        seen.add(key)
    for key, val in ctx.items():
        if key not in seen:
            ordered_ctx.append({'k': key, 'v': _val(val)})

    # Items: canonical field order; fill missing fields with 'nan'
    serial_items = []
    for item in items_raw:
        row = {}
        for f in ITEM_VARIABLES_ORDER:
            row[f] = _val(item.get(f))
        for f, v in item.items():
            if f not in row:
                row[f] = _val(v)
        serial_items.append(row)

    found_count = qs.count()
    return JsonResponse({
        'ok': True,
        'order_number': order.order_number,
        'customer_name': order.client or order.ship_name or '—',
        'found': found_count,
        'context': ordered_ctx,       # list of {k, v} in guide order
        'item_cols': ITEM_VARIABLES_ORDER,
        'items': serial_items,
        'items_count': len(serial_items),
    })
