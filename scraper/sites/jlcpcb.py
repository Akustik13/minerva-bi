"""
scraper/sites/jlcpcb.py — JLCPCB invoice scraper.

Flow:
  1. Відкриваємо jlcpcb.com
  2. Скрипт пробує натиснути Sign In автоматично
     Якщо не вийшло — просить тебе натиснути вручну
  3. Чекає поки відкриється passport.jlcpcb.com
  4. Автоматично вводить email + пароль
  5. Якщо є CAPTCHA — чекає поки вирішиш вручну
  6. Після входу → /user-center/payments/billing
  7. Для кожного рядку: клік Download → нова вкладка → page.pdf()
  8. Файли: output_dir/jlcpcb/jlcpcb_invoice_W20260909....pdf
"""
from datetime import datetime
from pathlib import Path
from base import BaseScraper, ScraperError

_BASE        = 'https://jlcpcb.com'
_BILLING_URL = f'{_BASE}/user-center/payments/billing'


class JLCScraper(BaseScraper):
    SITE_NAME = 'jlcpcb'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.output_dir = self.output_dir / 'jlcpcb'
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Логін ─────────────────────────────────────────────────────────────────

    # client_id статичний для jlcpcb.com
    _PASSPORT_URL = (
        'https://passport.jlcpcb.com/#/login'
        '?response_type=code'
        '&client_id=34495309ae47483ebf71827b5bcb591c'
        '&redirect_url=https%3A%2F%2Fjlcpcb.com%2Fapi%2Fauth%2Flogin'
        '&from=jlcpcb'
    )

    async def _is_logged_in(self, page) -> bool:
        """Перевіряємо чи збережена сесія ще дійсна."""
        self.log.info('Checking saved session...')
        await page.goto(_BILLING_URL, wait_until='domcontentloaded', timeout=20_000)
        # Чекаємо 7с — Vue.js auth check може редіректити із затримкою
        await page.wait_for_timeout(7000)
        url = page.url
        if 'passport' in url or 'login' in url:
            self.log.info('Session expired or not found. URL: %s', url[:80])
            return False
        # Додатково перевіряємо чи є елементи таблиці (не просто URL)
        has_table = await page.locator('table, .ant-table').count() > 0
        if not has_table:
            self.log.info('No billing table found — treating as not logged in')
            return False
        self.log.info('Session valid! URL: %s', url[:60])
        return True

    async def login(self, page) -> bool:
        self.log.info('Opening passport login page...')
        await page.goto(self._PASSPORT_URL, wait_until='domcontentloaded', timeout=30_000)
        await page.wait_for_timeout(2000)
        return await self._fill_login_form(page)

    async def _fill_login_form(self, page) -> bool:
        # Вводимо email
        email_sel = (
            'input[type=email], input[name=email], '
            'input[placeholder*=email i], input[placeholder*=mail i]'
        )
        try:
            await page.wait_for_selector(email_sel, timeout=10_000)
        except Exception:
            self.log.error('Email field not found. URL: %s', page.url)
            await self._screenshot(page, 'no_email_field')
            return False

        self.log.info('Filling email...')
        await page.fill(email_sel, self.username)
        await page.wait_for_timeout(500)

        # Next (якщо 2-step flow)
        next_btn = page.locator('button:has-text("Next"), button:has-text("Continue")')
        if await next_btn.count() > 0:
            await next_btn.first.click()
            await page.wait_for_timeout(1500)

        # Вводимо пароль
        try:
            await page.wait_for_selector('input[type=password]', timeout=8_000)
        except Exception:
            self.log.error('Password field not found')
            return False

        self.log.info('Filling password...')
        await page.fill('input[type=password]', self.password)
        await page.wait_for_timeout(800)

        # Натискаємо Submit
        submit_sel = (
            'button[type=submit], button:has-text("Log In"), '
            'button:has-text("Sign In"), button:has-text("Login")'
        )
        try:
            submit = page.locator(submit_sel)
            if await submit.count() > 0:
                await submit.first.click()
                self.log.info('Submit clicked')
        except Exception:
            pass

        await page.wait_for_timeout(2000)

        # Перевіряємо reCAPTCHA модалку ("Security Verification")
        captcha_modal = page.locator('text=Security Verification, text=I\'m not a robot')
        if await captcha_modal.count() > 0 or await page.locator('iframe[src*="recaptcha"]').count() > 0:
            self.log.info('reCAPTCHA detected — trying to click checkbox...')
            try:
                # Клікаємо чекбокс "I'm not a robot" в iframe
                frame = page.frame_locator('iframe[title*="reCAPTCHA"], iframe[src*="recaptcha"]').first
                checkbox = frame.locator('.recaptcha-checkbox-border, #recaptcha-anchor')
                if await checkbox.count() > 0:
                    await checkbox.click()
                    self.log.info('reCAPTCHA checkbox clicked — waiting for result...')
                    await page.wait_for_timeout(3000)
            except Exception as e:
                self.log.warning('Could not auto-click reCAPTCHA: %s', e)

            print('\n' + '=' * 55)
            print('⚠️  reCAPTCHA у браузері:')
            print('   1. Натисни "I\'m not a robot"')
            print('   2. Вирішуй задачу якщо з\'явиться')
            print('   3. Натисни Sign In — скрипт продовжить сам')
            print('=' * 55 + '\n')
        else:
            self.log.info('No CAPTCHA — waiting for redirect...')

        # Чекаємо поки URL іде з passport/login (макс. 2 хв)
        try:
            await page.wait_for_function(
                "() => !window.location.href.includes('passport')"
                "   && !window.location.href.includes('login')",
                timeout=120_000,
            )
        except Exception:
            await self._screenshot(page, 'login_failed')
            self.log.error('Login timeout. URL: %s', page.url)
            return False

        await page.wait_for_timeout(1500)
        self.log.info('Login successful! URL: %s', page.url[:70])
        return True

    # ── Cookie popup ──────────────────────────────────────────────────────────

    async def _accept_cookies(self, page):
        try:
            btn = page.locator(
                'button:has-text("Accept All"), button:has-text("Accept"), '
                'button:has-text("Reject All")'
            )
            if await btn.count() > 0:
                await btn.first.click()
                self.log.info('Cookie popup dismissed')
                await page.wait_for_timeout(500)
        except Exception:
            pass

    # ── Завантаження рахунків ─────────────────────────────────────────────────

    async def download_invoices(self, page, start_date: str, end_date: str) -> list[str]:
        # Якщо після логіну опинились не на білінгу — переходимо
        if 'billing' not in page.url:
            self.log.info('Navigating to billing page...')
            await page.goto(_BILLING_URL, wait_until='domcontentloaded', timeout=30_000)
            await page.wait_for_timeout(3000)

        await self._accept_cookies(page)
        await self._screenshot(page, 'billing_page')
        self.log.info('Billing URL: %s', page.url[:80])

        # Парсимо діапазон дат
        try:
            dt_start = datetime.strptime(start_date, '%Y-%m-%d')
            dt_end   = datetime.strptime(end_date,   '%Y-%m-%d')
        except ValueError:
            dt_start = dt_end = None

        return await self._collect_all_pages(page, dt_start, dt_end)

    # ── Збір по сторінках ────────────────────────────────────────────────────

    # URL інвойсу по batch номеру
    _INVOICE_URL = (
        'https://jlcpcb.com/user-center/invoice/'
        '?batchNum={batch}&type=myOrdersBatchSecendLevelRecord'
    )

    async def _collect_all_pages(self, page, dt_start, dt_end) -> list[str]:
        downloaded = []
        page_num   = 1
        stop       = False

        while not stop:
            self.log.info('Processing billing page %d...', page_num)
            await page.wait_for_timeout(1500)

            rows = await self._get_table_rows(page)
            if not rows:
                self.log.warning('No rows on page %d', page_num)
                await self._screenshot(page, f'page_{page_num}_empty')
                break

            self.log.info('Found %d row(s)', len(rows))

            for batch_num, date_str in rows:
                # Фільтр дат
                if dt_start and date_str:
                    try:
                        row_dt = datetime.strptime(date_str[:10], '%Y-%m-%d')
                        if row_dt < dt_start:
                            self.log.info('Row %s before start_date — stopping', batch_num)
                            stop = True
                            break
                        if row_dt > dt_end:
                            self.log.info('Row %s after end_date — skip', batch_num)
                            continue
                    except ValueError:
                        pass

                path = await self._save_invoice(page, batch_num)
                if path:
                    downloaded.append(path)

            if stop:
                break
            if not await self._go_next_page(page):
                break
            page_num += 1
            if page_num > 50:
                break

        return downloaded

    async def _get_table_rows(self, page) -> list[tuple]:
        """Повертає список (batch_num, date_str) з таблиці білінгу."""
        rows = []
        tr_loc = page.locator('table tbody tr, .ant-table-tbody tr')
        count  = await tr_loc.count()

        for i in range(count):
            cells = tr_loc.nth(i).locator('td')
            n     = await cells.count()
            if n < 2:
                continue

            batch_num = (await cells.nth(0).inner_text()).strip()
            if not batch_num.startswith('W'):
                continue

            date_str = ''
            for j in range(n):
                text = (await cells.nth(j).inner_text()).strip()
                if len(text) >= 10 and text[4:5] == '-' and text[7:8] == '-':
                    date_str = text
                    break

            rows.append((batch_num, date_str))

        return rows

    async def _save_invoice(self, page, batch_num: str) -> str | None:
        """
        Відкриває сторінку інвойсу → натискає Download → зберігає PDF з сервера.
        Це правильний підхід — JLCPCB генерує PDF на сервері.
        """
        safe    = batch_num.strip()
        out_dir = self.output_dir

        # Пропускаємо якщо вже є
        existing = list(out_dir.glob(f'*{safe}*'))
        if existing:
            self.log.info('Skip (exists): %s', safe)
            return str(existing[0])

        invoice_url = self._INVOICE_URL.format(batch=safe)
        self.log.info('Opening invoice page: %s', safe)

        inv_page = await page.context.new_page()
        try:
            await inv_page.goto(invoice_url, wait_until='domcontentloaded', timeout=30_000)
            await inv_page.wait_for_timeout(3000)

            # Перевіряємо чи не редірект на логін
            if 'passport' in inv_page.url or 'login' in inv_page.url:
                self.log.warning('Invoice page redirected to login — session expired: %s', safe)
                await self._screenshot(inv_page, f'invoice_{safe}_login')
                return None

            # Закриваємо cookie popup якщо є
            await self._accept_cookies(inv_page)

            # Чекаємо поки Vue.js відрендерить контент
            # JLCPCB використовує Vue — DOM може бути порожнім одразу після load
            dl_btn = None
            for attempt in range(3):
                await inv_page.wait_for_timeout(2000)

                # Спробуємо різні селектори для DOWNLOAD кнопки
                selectors = [
                    ':text("DOWNLOAD")',
                    'button:has-text("download")',
                    'a:has-text("download")',
                    '[class*="download"]:not([class*="app"])',
                    'span:has-text("DOWNLOAD")',
                ]
                for sel in selectors:
                    try:
                        loc = inv_page.locator(sel).first
                        if await loc.is_visible():
                            dl_btn = loc
                            self.log.info('Found btn with selector "%s" (attempt %d)', sel, attempt + 1)
                            break
                    except Exception:
                        continue

                if dl_btn:
                    break
                self.log.info('Btn not found yet, waiting... (attempt %d/3)', attempt + 1)
                if attempt == 0:
                    await self._screenshot(inv_page, f'invoice_{safe}_loading')

            if not dl_btn:
                await self._screenshot(inv_page, f'invoice_{safe}_no_btn')
                self.log.warning('No DOWNLOAD button on invoice page: %s', safe)
                return None

            self.log.info('Clicking DOWNLOAD for %s...', safe)
            async with inv_page.expect_download(timeout=30_000) as dl_info:
                await dl_btn.click()

            dl        = await dl_info.value
            suggested = dl.suggested_filename or 'invoice.pdf'
            ext       = Path(suggested).suffix or '.pdf'
            fname     = f'invoice_{safe}{ext}'
            save_path = out_dir / fname
            await dl.save_as(str(save_path))
            self.log.info('Saved: %s', save_path)
            return str(save_path)

        except Exception as e:
            self.log.warning('Failed %s: %s', safe, e)
            await self._screenshot(inv_page, f'invoice_{safe}_error')
            return None
        finally:
            try:
                await inv_page.close()
            except Exception:
                pass

    async def _go_next_page(self, page) -> bool:
        nxt = page.locator(
            'li.ant-pagination-next:not(.ant-pagination-disabled) button, '
            '[aria-label="Next Page"]:not([disabled])'
        )
        if await nxt.count() == 0:
            return False
        try:
            await nxt.first.click()
            await page.wait_for_timeout(2000)
            return True
        except Exception:
            return False
