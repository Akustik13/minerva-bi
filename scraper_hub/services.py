"""
scraper_hub/services.py — business logic для запуску scraper'ів.
"""
import asyncio
import logging
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)

_SCRAPER_DIR = Path(settings.BASE_DIR) / 'scraper'


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
    """Базова папка для всіх PDF в media/."""
    d = Path(settings.MEDIA_ROOT) / 'scraper'
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Основна функція ────────────────────────────────────────────────────────────

def run_site(config, triggered_by: str = 'manual'):
    """
    Запускає scraper для одного ScraperSiteConfig.
    Створює ScraperRun + ScraperDocument, сповіщає, повертає ScraperRun.
    """
    from scraper_hub.models import ScraperRun, ScraperDocument

    run = ScraperRun.objects.create(
        config=config,
        triggered_by=triggered_by,
        status='running',
    )
    config.last_run_at     = run.started_at
    config.last_run_status = 'running'
    config.save(update_fields=['last_run_at', 'last_run_status'])

    log_lines = []

    def _log(msg):
        log.info(msg)
        log_lines.append(msg)

    try:
        out_root   = _output_root()
        end_dt     = date.today()
        start_dt   = end_dt - timedelta(days=config.lookback_days)

        _log(f'[{config.get_site_name_display()}] start={start_dt} end={end_dt}')

        ScraperCls = _get_scraper_class(config.site_name)
        scraper    = ScraperCls(
            username=config.username,
            password=config.password,
            output_dir=str(out_root),  # scraper додає site_name сам
            headless=True,
        )

        # asyncio.run() всередині потоку — безпечно
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

        _log(f'Завантажено {len(files)} файл(ів)')

        media_root = Path(settings.MEDIA_ROOT)
        for fpath in files:
            p = Path(fpath)
            if not p.exists():
                _log(f'Файл не знайдено: {p}')
                continue

            # Відносний шлях для FileField
            try:
                rel = p.relative_to(media_root)
            except ValueError:
                rel = Path('scraper') / config.site_name / p.name

            batch = p.stem.replace('invoice_', '')

            # Не створювати дублікат якщо вже є
            if ScraperDocument.objects.filter(batch_num=batch).exists():
                _log(f'Skip duplicate: {batch}')
                continue

            doc = ScraperDocument.objects.create(
                run=run,
                batch_num=batch,
                file=str(rel),
            )
            if config.auto_create_expense:
                _create_expense(config, doc, p)

        run.files_downloaded = len(files)
        run.status           = 'ok' if files else 'partial'
        run.finished_at      = timezone.now()
        run.log_output       = '\n'.join(log_lines)
        run.save()

        config.last_run_status = run.status
        config.last_run_files  = run.files_downloaded
        config.save(update_fields=['last_run_status', 'last_run_files'])

    except Exception as exc:
        log.exception('Scraper run failed: %s', exc)
        run.status        = 'error'
        run.error_message = str(exc)
        run.finished_at   = timezone.now()
        run.log_output    = '\n'.join(log_lines)
        run.save()
        config.last_run_status = 'error'
        config.save(update_fields=['last_run_status'])

    _notify(config, run)
    return run


def run_site_in_thread(config, triggered_by: str = 'manual'):
    """Запускає run_site у фоновому потоці, повертає одразу."""
    t = threading.Thread(
        target=run_site,
        args=(config,),
        kwargs={'triggered_by': triggered_by},
        daemon=True,
    )
    t.start()
    return t


# ── Бухгалтерія ───────────────────────────────────────────────────────────────

def _create_expense(config, doc, pdf_path: Path):
    try:
        from accounting.models import Expense
        from django.core.files import File

        exp = Expense(
            date=date.today(),
            amount=0,
            currency='USD',
            supplier=config.supplier,
            category=config.expense_category,
            description=f'{config.get_site_name_display()} invoice {doc.batch_num}',
            is_vat_deductible=False,
        )
        with open(pdf_path, 'rb') as f:
            exp.receipt.save(pdf_path.name, File(f), save=False)
        exp.save()
        doc.expense = exp
        doc.save(update_fields=['expense'])
    except Exception as e:
        log.warning('Не вдалося створити Expense для %s: %s', doc.batch_num, e)


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
