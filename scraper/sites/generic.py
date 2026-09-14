"""
scraper/sites/generic.py — Generic configurable scraper.

Читає налаштування з ScraperSiteConfig (CSS selectors, URLs) замість
хардкодених значень. Використовується для site_name='custom'.

Flow:
  1. BaseScraper.run_async() відкриває браузер, викликає login() + download_invoices()
  2. login() — заповнює форму за CSS selectors
  3. download_invoices() — ітерує рядки таблиці, завантажує PDF
"""
from datetime import datetime
from pathlib import Path
from base import BaseScraper, ScraperError


class GenericScraper(BaseScraper):
    SITE_NAME = 'custom'

    def __init__(self, *args,
                 login_url: str = '',
                 invoice_list_url: str = '',
                 email_selector: str = 'input[type=email]',
                 password_selector: str = 'input[type=password]',
                 submit_selector: str = 'button[type=submit]',
                 login_success_url: str = '',
                 row_selector: str = 'table tbody tr',
                 batch_col_index: int = 0,
                 date_col_index: int = 2,
                 download_selector: str = '',
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.login_url         = login_url
        self.invoice_list_url  = invoice_list_url
        self.email_selector    = email_selector
        self.password_selector = password_selector
        self.submit_selector   = submit_selector
        self.login_success_url = login_success_url
        self.row_selector      = row_selector
        self.batch_col_index   = batch_col_index
        self.date_col_index    = date_col_index
        self.download_selector = download_selector

        self.output_dir = self.output_dir / 'custom'
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Логін (реалізація абстрактного методу) ────────────────────────────────

    async def login(self, page) -> bool:
        if not self.login_url:
            self.log.warning('login_url не налаштовано — пропускаємо логін')
            return True

        self.log.info('Відкриваємо сторінку логіну: %s', self.login_url)
        await page.goto(self.login_url, wait_until='domcontentloaded', timeout=30_000)
        await page.wait_for_timeout(2_000)

        try:
            await page.wait_for_selector(self.email_selector, timeout=8_000)
            await page.fill(self.email_selector, self.username)
        except Exception:
            self.log.warning('Поле email не знайдено: %s', self.email_selector)

        try:
            await page.wait_for_selector(self.password_selector, timeout=5_000)
            await page.fill(self.password_selector, self.password)
        except Exception:
            self.log.warning('Поле пароля не знайдено: %s', self.password_selector)

        try:
            await page.click(self.submit_selector)
            await page.wait_for_timeout(3_000)
        except Exception as e:
            self.log.error('Не вдалося натиснути кнопку входу: %s', e)
            return False

        if self.login_success_url:
            ok = self.login_success_url.lower() in page.url.lower()
        else:
            ok = (not self.login_url) or (self.login_url.lower() not in page.url.lower())

        if ok:
            self.log.info('Логін успішний. URL: %s', page.url[:80])
        else:
            self.log.error('Логін не вдався. URL: %s', page.url[:80])
        return ok

    # ── Завантаження (реалізація абстрактного методу) ─────────────────────────

    async def download_invoices(self, page, start_date: str, end_date: str) -> list[str]:
        if not self.invoice_list_url:
            raise ScraperError('invoice_list_url не налаштовано')

        start_dt = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_dt   = datetime.strptime(end_date,   '%Y-%m-%d').date()

        self.log.info('Відкриваємо список рахунків: %s', self.invoice_list_url)
        await page.goto(self.invoice_list_url, wait_until='domcontentloaded', timeout=30_000)
        await page.wait_for_timeout(3_000)

        try:
            await page.wait_for_selector(self.row_selector, timeout=10_000)
        except Exception:
            self.log.warning('Рядки таблиці не знайдено: %s', self.row_selector)
            return []

        rows = await page.query_selector_all(self.row_selector)
        self.log.info('Знайдено рядків: %d', len(rows))

        downloaded = []
        context = page.context

        for row in rows:
            fpath, batch, date_str = await self._download_row(page, row, context)
            if not fpath:
                continue

            # Filter by date range if date is available
            if date_str:
                row_dt = None
                for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%m/%d/%Y', '%d/%m/%Y', '%Y/%m/%d'):
                    try:
                        row_dt = datetime.strptime(date_str.strip(), fmt).date()
                        break
                    except ValueError:
                        continue
                if row_dt and not (start_dt <= row_dt <= end_dt):
                    self.log.info('Поза діапазоном [%s]: %s', date_str, batch)
                    continue

            downloaded.append(fpath)

        return downloaded

    # ── Внутрішній помічник ───────────────────────────────────────────────────

    async def _download_row(self, page, row, context) -> tuple:
        """Returns (filepath | None, batch_str, date_str)."""
        cells = await row.query_selector_all('td, th')

        batch = ''
        if len(cells) > self.batch_col_index:
            batch = (await cells[self.batch_col_index].inner_text()).strip()

        date_str = ''
        if len(cells) > self.date_col_index:
            date_str = (await cells[self.date_col_index].inner_text()).strip()

        if not batch:
            return None, batch, date_str

        if not self.download_selector:
            self.log.warning('download_selector не налаштовано — пропускаємо рядок %s', batch)
            return None, batch, date_str

        try:
            dl_btn = await row.query_selector(self.download_selector)
            if not dl_btn:
                self.log.warning('Кнопка завантаження не знайдена для %s', batch)
                return None, batch, date_str

            fpath = self.output_dir / f'invoice_{batch}.pdf'

            async with context.expect_page() as new_page_info:
                await dl_btn.click()

            try:
                new_page = await new_page_info.value
                await new_page.wait_for_load_state('domcontentloaded', timeout=15_000)
                await new_page.pdf(path=str(fpath))
                await new_page.close()
                self.log.info('✅ Завантажено: invoice_%s.pdf', batch)
                return str(fpath), batch, date_str
            except Exception as e:
                self.log.warning('PDF з нової вкладки для %s: %s', batch, e)
                return None, batch, date_str

        except Exception as e:
            self.log.warning('Помилка завантаження для %s: %s', batch, e)
            return None, batch, date_str
