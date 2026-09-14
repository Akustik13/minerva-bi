"""
scraper_hub/services.py — business logic для запуску scraper'ів.
"""
import asyncio
import logging
import os
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

_SCRAPER_DIR  = Path(settings.BASE_DIR) / 'scraper'
_WORKER_URL   = os.environ.get('SCRAPER_WORKER_URL', '').rstrip('/')   # e.g. http://scraper-worker:8888
_VNC_URL      = os.environ.get('SCRAPER_VNC_URL', '')                  # e.g. http://192.168.2.123:6080


def using_worker() -> bool:
    """True якщо Django запущено в Docker і є scraper-worker сервіс."""
    return bool(_WORKER_URL)


def _ensure_scraper_path():
    if str(_SCRAPER_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRAPER_DIR))


def _get_scraper_class(site_name: str):
    _ensure_scraper_path()
    if site_name == 'jlcpcb':
        from sites.jlcpcb import JLCScraper   # noqa: PLC0415
        return JLCScraper
    if site_name == 'ups':
        from sites.ups import UPSScraper       # noqa: PLC0415
        return UPSScraper
    raise ValueError(f'Невідомий сайт: {site_name}')


def _output_root() -> Path:
    d = Path(settings.MEDIA_ROOT) / 'scraper'
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Live log handler — пише в БД кожні 3 секунди ─────────────────────────────

class _DBLogHandler(logging.Handler):
    """Перехоплює логи scraper.* і зберігає їх в ScraperRun.log_output."""

    def __init__(self, run_id: int):
        super().__init__()
        self.run_id     = run_id
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        self.setFormatter(logging.Formatter('%(asctime)s  %(message)s', datefmt='%H:%M:%S'))

    def emit(self, record):
        try:
            self._buf.append(self.format(record))
            if time.monotonic() - self._last_flush >= 3:
                self._flush()
        except Exception:
            pass

    def _flush(self):
        if not self._buf:
            return
        from scraper_hub.models import ScraperRun
        new_text = '\n'.join(self._buf)
        self._buf.clear()
        self._last_flush = time.monotonic()
        try:
            run = ScraperRun.objects.get(pk=self.run_id)
            run.log_output = (run.log_output + '\n' + new_text).lstrip('\n')
            run.save(update_fields=['log_output'])
        except Exception:
            pass

    def flush_final(self):
        self._flush()


# ── Основна функція ───────────────────────────────────────────────────────────

def run_site(run, config=None):
    """
    Запускає scraper для вже існуючого ScraperRun.
    config — береться з run.config якщо не передано явно.
    """
    from scraper_hub.models import ScraperDocument

    if config is None:
        config = run.config

    # Прикріплюємо live-log handler до scraper-логера
    db_handler = _DBLogHandler(run.pk)
    scraper_logger = logging.getLogger('scraper')
    scraper_logger.addHandler(db_handler)
    scraper_logger.setLevel(logging.INFO)

    def _log(msg):
        log.info(msg)
        db_handler._buf.append(f'{time.strftime("%H:%M:%S")}  {msg}')

    try:
        out_root = _output_root()
        end_dt   = date.today()
        start_dt = end_dt - timedelta(days=config.lookback_days)

        _log(f'▶ Старт [{config.get_site_name_display()}]  {start_dt} → {end_dt}')
        db_handler._flush()

        ScraperCls = _get_scraper_class(config.site_name)
        scraper    = ScraperCls(
            username=config.username,
            password=config.password,
            output_dir=str(out_root),
            headless=True,
        )

        loop = asyncio.new_event_loop()
        try:
            files = loop.run_until_complete(
                scraper.run_async(
                    start_dt.strftime('%Y-%m-%d'),
                    end_dt.strftime('%Y-%m-%d'),
                )
            )
        finally:
            loop.close()

        _log(f'✅ Завантажено {len(files)} файл(ів)')

        media_root = Path(settings.MEDIA_ROOT)
        for fpath in files:
            p = Path(fpath)
            if not p.exists():
                _log(f'⚠ Файл не знайдено: {p}')
                continue
            try:
                rel = p.relative_to(media_root)
            except ValueError:
                rel = Path('scraper') / config.site_name / p.name

            batch = p.stem.replace('invoice_', '')
            if ScraperDocument.objects.filter(batch_num=batch).exists():
                _log(f'↩ Вже є: {batch}')
                continue

            _, jlc_order = _get_order_amount(batch)
            doc = ScraperDocument.objects.create(
                run=run, batch_num=batch, file=str(rel),
                jlc_order=jlc_order,
            )
            if config.auto_create_expense:
                _create_expense(config, doc, p)

        run.files_downloaded = len(files)
        run.status           = 'ok' if files else 'partial'
        run.finished_at      = timezone.now()

    except Exception as exc:
        log.exception('Scraper run failed: %s', exc)
        _log(f'❌ Помилка: {exc}')
        run.status        = 'error'
        run.error_message = str(exc)
        run.finished_at   = timezone.now()

    finally:
        db_handler.flush_final()
        scraper_logger.removeHandler(db_handler)
        run.save()
        config.last_run_at     = run.started_at
        config.last_run_status = run.status
        config.last_run_files  = run.files_downloaded
        config.save(update_fields=['last_run_at', 'last_run_status', 'last_run_files'])

    _notify(config, run)
    return run


def create_and_run_in_thread(config, triggered_by: str = 'manual'):
    """
    Створює ScraperRun, запускає у фоні, повертає run одразу.
    На NAS (Docker): викликає scraper-worker API + опитує статус.
    На Windows (dev): запускає scraper напряму в потоці.
    """
    from scraper_hub.models import ScraperRun

    run = ScraperRun.objects.create(
        config=config,
        triggered_by=triggered_by,
        status='running',
        log_output=f'{time.strftime("%H:%M:%S")}  ⚙️ Ініціалізація...',
    )
    config.last_run_at     = run.started_at
    config.last_run_status = 'running'
    config.save(update_fields=['last_run_at', 'last_run_status'])

    if using_worker():
        target = _run_via_worker_api
    else:
        target = run_site

    t = threading.Thread(target=target, args=(run,), daemon=True)
    t.start()
    return run


def _run_via_worker_api(run):
    """Делегує запуск до scraper-worker Docker сервісу через HTTP API."""
    import requests as req_lib

    config   = run.config
    end_dt   = date.today()
    start_dt = end_dt - timedelta(days=config.lookback_days)

    def _log(msg):
        run.log_output = (run.log_output + f'\n{time.strftime("%H:%M:%S")}  {msg}').lstrip()
        run.save(update_fields=['log_output'])

    try:
        _log(f'▶ Відправляємо запит до scraper-worker...')

        resp = req_lib.post(
            f'{_WORKER_URL}/run',
            json={
                'site':     config.site_name,
                'username': config.username,
                'password': config.password,
                'start':    start_dt.strftime('%Y-%m-%d'),
                'end':      end_dt.strftime('%Y-%m-%d'),
                'output':   '/media/scraper',
            },
            timeout=15,
        )
        if resp.status_code == 409:
            raise Exception('Scraper вже виконується в контейнері')
        resp.raise_for_status()

        if _VNC_URL:
            _log(f'🖥  Браузер доступний: {_VNC_URL}/vnc.html')

        # Опитуємо статус поки виконується
        while True:
            time.sleep(3)
            try:
                st = req_lib.get(f'{_WORKER_URL}/status', timeout=5).json()
            except Exception:
                continue

            run.log_output = st.get('log', run.log_output)
            run.save(update_fields=['log_output'])

            if not st.get('running'):
                break

        # Обробляємо завантажені файли.
        # Worker повертає шляхи як /media/scraper/... (всередині свого контейнера).
        # Django бачить той самий volume як settings.MEDIA_ROOT (/app/media/ або /media/).
        # Перетворюємо: /media/scraper/jlcpcb/foo.pdf → <MEDIA_ROOT>/scraper/jlcpcb/foo.pdf
        from scraper_hub.models import ScraperDocument
        files      = st.get('files', [])
        media_root = Path(settings.MEDIA_ROOT)

        def _resolve_worker_path(fpath: str) -> Path:
            """Конвертує шлях з worker-контейнера в локальний шлях Django."""
            p = Path(fpath)
            # Worker монтує volume як /media; Django монтує як MEDIA_ROOT
            # Шукаємо 'scraper' в компонентах і беремо відносний шлях від нього
            parts = p.parts
            try:
                idx = parts.index('scraper')
                rel = Path(*parts[idx:])          # scraper/jlcpcb/invoice_XXX.pdf
                return media_root / rel
            except ValueError:
                return media_root / p.name        # fallback

        for fpath in files:
            local_p = _resolve_worker_path(fpath)
            rel     = local_p.relative_to(media_root) if local_p.is_relative_to(media_root) else Path('scraper') / config.site_name / local_p.name

            batch = local_p.stem.replace('invoice_', '')
            if ScraperDocument.objects.filter(batch_num=batch).exists():
                continue

            _, jlc_order = _get_order_amount(batch)
            doc = ScraperDocument.objects.create(
                run=run, batch_num=batch, file=str(rel),
                jlc_order=jlc_order,
            )
            if config.auto_create_expense and local_p.exists():
                _create_expense(config, doc, local_p)

        run.files_downloaded = len(files)
        run.status           = st.get('status', 'ok') if files else 'partial'
        run.finished_at      = timezone.now()
        run.save()

        config.last_run_status = run.status
        config.last_run_files  = run.files_downloaded
        config.save(update_fields=['last_run_status', 'last_run_files'])

    except Exception as exc:
        log.exception('Worker API error: %s', exc)
        run.status        = 'error'
        run.error_message = str(exc)
        run.finished_at   = timezone.now()
        run.save()
        config.last_run_status = 'error'
        config.save(update_fields=['last_run_status'])

    _notify(config, run)


# ── Бухгалтерія ───────────────────────────────────────────────────────────────

def _extract_pdf_amount(pdf_path: Path):
    """Extract total invoice amount from a JLCPCB PDF using pypdf. Returns Decimal or None."""
    import re
    from decimal import Decimal, InvalidOperation
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        # JLCPCB invoice PDF patterns (grand total / amount due / total amount)
        patterns = [
            r'(?:Grand\s+Total|Total\s+Amount|Amount\s+Due)[^\d$]*\$?\s*([\d,]+\.?\d*)',
            r'(?:Grand\s+Total|Total\s+Amount|Amount\s+Due)[^\n]*?USD\s+([\d,]+\.?\d*)',
            r'Total[:\s]+USD\s+([\d,]+\.?\d*)',
            r'Total[:\s]+\$\s*([\d,]+\.?\d*)',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val = m.group(1).replace(',', '')
                try:
                    return Decimal(val)
                except InvalidOperation:
                    continue
    except Exception as e:
        log.debug('PDF parse error for %s: %s', pdf_path.name, e)
    return None


def _get_order_amount(batch_num: str):
    """
    Try to get invoice total from JLCOrder.total_price (already stored in DB from API sync).
    Falls back to calling the API directly if total_price is blank.
    Returns (Decimal | None, JLCOrder | None).
    """
    from decimal import Decimal
    try:
        from jlcpcb.models import JLCOrder
        order = JLCOrder.objects.filter(jlc_order_number=batch_num).first()
        if not order:
            return None, None

        if order.total_price:
            return order.total_price, order

        # total_price not cached — try API
        try:
            from jlcpcb.services.api import JLCAPIClient
            client = JLCAPIClient.from_config()
            raw    = client.get_pcb_order(batch_num)
            # Try common top-level amount fields returned by JLCPCB API
            for field in ('orderAmount', 'totalAmount', 'payAmount', 'totalFee'):
                val = raw.get(field)
                if val is not None:
                    amount = Decimal(str(val))
                    order.total_price = amount
                    order.save(update_fields=['total_price'])
                    return amount, order
        except Exception as api_err:
            log.debug('JLCPCB API amount lookup failed for %s: %s', batch_num, api_err)

        return None, order
    except Exception as e:
        log.debug('JLCOrder lookup error for %s: %s', batch_num, e)
        return None, None


def _create_expense(config, doc, pdf_path: Path):
    try:
        from accounting.models import Expense
        from django.core.files import File

        # 1. Amount from API (stored on JLCOrder or fetched live)
        api_amount, jlc_order = _get_order_amount(doc.batch_num)

        # 2. Amount from PDF
        pdf_amount = _extract_pdf_amount(pdf_path) if pdf_path and pdf_path.exists() else None

        # Choose amount: prefer API; use PDF as fallback; log discrepancy
        amount = api_amount or pdf_amount or 0
        if api_amount and pdf_amount and abs(api_amount - pdf_amount) > 1:
            log.warning(
                'Amount mismatch for %s: API=%s PDF=%s — using API value',
                doc.batch_num, api_amount, pdf_amount,
            )

        exp = Expense(
            date=date.today(),
            amount=amount,
            currency='USD',
            supplier=config.supplier,
            category=config.expense_category,
            description=f'{config.get_site_name_display()} invoice {doc.batch_num}',
            is_vat_deductible=False,
        )
        with open(pdf_path, 'rb') as f:
            exp.receipt.save(pdf_path.name, File(f), save=False)
        exp.save()

        update_fields = ['expense']
        doc.expense = exp
        if jlc_order:
            doc.jlc_order = jlc_order
            update_fields.append('jlc_order')
        if amount:
            doc.amount = amount
            update_fields.append('amount')
        doc.save(update_fields=update_fields)

    except Exception as e:
        log.warning('Не вдалося створити Expense для %s: %s', doc.batch_num, e)


# ── Для management command (синхронний запуск) ────────────────────────────────

def run_site_sync(config, triggered_by: str = 'cron'):
    from scraper_hub.models import ScraperRun
    run = ScraperRun.objects.create(
        config=config,
        triggered_by=triggered_by,
        status='running',
    )
    config.last_run_at     = run.started_at
    config.last_run_status = 'running'
    config.save(update_fields=['last_run_at', 'last_run_status'])
    return run_site(run)


# ── Сповіщення ────────────────────────────────────────────────────────────────

def _notify(config, run):
    try:
        from config.models import NotificationSettings
        ns = NotificationSettings.objects.first()
        if not ns:
            return

        ok    = run.status in ('ok', 'partial')
        emoji = '✅' if ok else '❌'
        site  = config.get_site_name_display()
        subj  = f'{emoji} {site} Scraper — {run.files_downloaded} рахунків'
        body  = (
            f'Сайт: {site}\n'
            f'Статус: {run.get_status_display()}\n'
            f'Файлів: {run.files_downloaded}\n'
            f'Час: {run.started_at:%Y-%m-%d %H:%M}\n'
        )
        if run.error_message:
            body += f'Помилка: {run.error_message[:300]}\n'

        if config.notify_email and getattr(ns, 'smtp_host', None) and getattr(ns, 'alert_email', None):
            _send_email(ns, subj, body)
        if config.notify_telegram and getattr(ns, 'telegram_bot_token', None):
            _send_telegram(ns, f'{subj}\n\n{body}')
    except Exception as e:
        log.warning('Notification error: %s', e)


def _send_email(ns, subject: str, body: str):
    from django.core.mail import send_mail
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [ns.alert_email], fail_silently=True)
    except Exception as e:
        log.warning('Email failed: %s', e)


def _send_telegram(ns, text: str):
    import requests  # noqa: PLC0415
    url = f'https://api.telegram.org/bot{ns.telegram_bot_token}/sendMessage'
    try:
        requests.post(url, json={'chat_id': ns.telegram_chat_id, 'text': text}, timeout=10)
    except Exception as e:
        log.warning('Telegram failed: %s', e)
