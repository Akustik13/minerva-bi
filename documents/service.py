"""
documents/service.py
Генерація .docx і PDF з шаблонів.
"""
import logging
import os
import subprocess
import tempfile
from io import BytesIO

from django.core.files.base import ContentFile
from django.utils import timezone

logger = logging.getLogger('documents')


def generate_docx(template, context: dict,
                  source_module='', source_object_id=None,
                  source_repr='', user=None) -> 'GeneratedDocument':
    """
    Генерує .docx файл з Word шаблону і context словника.
    Зберігає на сервері і повертає GeneratedDocument.
    """
    from documents.models import GeneratedDocument
    from docxtpl import DocxTemplate

    doc_obj = GeneratedDocument.objects.create(
        template=template,
        source_module=source_module,
        source_object_id=source_object_id,
        source_repr=source_repr,
        status='generating',
        generated_by=user,
    )

    try:
        tpl = DocxTemplate(template.template_file.path)
        tpl.render(context)

        buf = BytesIO()
        tpl.save(buf)
        buf.seek(0)

        safe  = source_repr.replace(' ', '_').replace('/', '-').replace('#', '')[:40]
        ts    = timezone.now().strftime('%Y%m%d_%H%M')
        fname = f'{template.doc_type}_{safe}_{ts}.docx'

        doc_obj.docx_file.save(fname, ContentFile(buf.getvalue()), save=False)
        doc_obj.status = 'ready'
        doc_obj.save()
        logger.info('DOCX generated: %s', fname)

        # Спроба конвертації в PDF через LibreOffice
        try:
            pdf_bytes = _convert_to_pdf(doc_obj.docx_file.path)
            if pdf_bytes:
                pdf_name = fname.replace('.docx', '.pdf')
                doc_obj.pdf_file.save(pdf_name, ContentFile(pdf_bytes), save=True)
                logger.info('PDF generated: %s', pdf_name)
        except Exception as pdf_err:
            logger.warning('PDF conversion skipped: %s', pdf_err)

        return doc_obj

    except Exception as e:
        doc_obj.status    = 'error'
        doc_obj.error_msg = str(e)
        doc_obj.save()
        logger.error('Document generation error: %s', e)
        raise


def _convert_to_pdf(docx_path: str) -> bytes | None:
    """Конвертувати .docx в PDF через LibreOffice.

    Використовує ізольований профіль LibreOffice (--env:UserInstallation) і
    writer_pdf_Export з вбудованими шрифтами щоб мінімізувати розбіжності
    форматування між Word і PDF.
    """
    candidates = (
        'libreoffice', 'soffice',
        '/usr/bin/libreoffice', '/usr/lib/libreoffice/program/soffice',
    )
    for cmd in candidates:
        if os.path.exists(cmd) or _command_exists(cmd):
            with tempfile.TemporaryDirectory() as tmpdir:
                # Isolated user profile — prevents stale state / lock-file issues
                # and ensures reproducible output across concurrent calls
                profile_url = 'file://' + tmpdir.replace('\\', '/') + '/lo_profile'
                result = subprocess.run(
                    [
                        cmd,
                        '--headless',
                        '--norestore',
                        '--nofirststartwizard',
                        f'--env:UserInstallation={profile_url}',
                        '--convert-to',
                        # Embed fonts → prevents glyph substitution that causes reflow
                        'pdf:writer_pdf_Export:EmbedStandardFonts=true,SelectPdfVersion=0',
                        '--outdir', tmpdir,
                        docx_path,
                    ],
                    capture_output=True, timeout=60,
                    env={**os.environ, 'HOME': tmpdir},
                )
                if result.returncode == 0:
                    pdf_name = os.path.splitext(os.path.basename(docx_path))[0] + '.pdf'
                    pdf_path = os.path.join(tmpdir, pdf_name)
                    if os.path.exists(pdf_path):
                        with open(pdf_path, 'rb') as f:
                            return f.read()
                else:
                    logger.warning(
                        'LibreOffice PDF stderr: %s',
                        result.stderr.decode('utf-8', errors='replace')[:500],
                    )
    return None


def _command_exists(cmd: str) -> bool:
    try:
        result = subprocess.run(['which', cmd], capture_output=True, timeout=3)
        return result.returncode == 0
    except Exception:
        return False
