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

        # Copy to media/orders/{source}/{order_number}/ so it shows in "Завантажені документи"
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
            copy_url = (
                f'{_s.MEDIA_URL}orders/{source_slug}/{order_number}/{copy_filename}'
            )

        return JsonResponse({
            'ok':           True,
            'doc_id':       doc.pk,
            'status':       doc.status,
            'docx_url':     f'/documents/download/{doc.pk}/docx/',
            'has_pdf':      bool(doc.pdf_file),
            'pdf_url':      f'/documents/download/{doc.pk}/pdf/' if doc.pdf_file else None,
            'filename':     doc.docx_file.name.split('/')[-1] if doc.docx_file else '',
            'file_size':    doc.file_size_display(),
            # Local-save data (MinervaLocalSave.saveUrlToFolder)
            'url':          copy_url,
            'copy_filename': copy_filename,
            'source_slug':  source_slug,
            'date_str':     _date.today().strftime('%Y-%m-%d'),
            'order_number': order_number,
        })
    except Exception as e:
        logger.error('generate_for_order %s: %s', order_pk, e)
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


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
        doc.delete_files()
        doc.delete()
        return JsonResponse({'ok': True})
    except Exception as e:
        return JsonResponse({'ok': False, 'error': str(e)})
