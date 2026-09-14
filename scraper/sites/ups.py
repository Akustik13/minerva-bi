"""
scraper/sites/ups.py — UPS Billing portal scraper (заготовка).

Поки що stub — детальна реалізація після тестування JLCPCB.
"""
from base import BaseScraper, ScraperError

_BILLING_URL = 'https://billing.ups.com/'


class UPSScraper(BaseScraper):
    SITE_NAME = 'ups'

    async def login(self, page) -> bool:
        self.log.info('Opening UPS Billing portal...')
        await page.goto(_BILLING_URL, wait_until='domcontentloaded', timeout=30_000)
        await page.wait_for_timeout(2000)

        # UPS SSO redirect → idp.login.ups.com
        if 'login' in page.url.lower() or 'idp' in page.url.lower():
            self.log.info('Filling UPS SSO login form...')

            email_sel = 'input[type=email], input[id*=email i], input[id*=user i]'
            try:
                await page.wait_for_selector(email_sel, timeout=10_000)
            except Exception:
                self.log.error('UPS login form not found. URL: %s', page.url)
                return False

            await page.fill(email_sel, self.username)
            await page.wait_for_timeout(500)

            # 2-step flow (email → Next → password)
            next_btn = page.locator('button:has-text("Next"), button[id*=next i]')
            if await next_btn.count() > 0:
                await next_btn.first.click()
                await page.wait_for_timeout(1500)

            await page.wait_for_selector('input[type=password]', timeout=8_000)
            await page.fill('input[type=password]', self.password)
            await page.wait_for_timeout(400)

            submit = 'button[type=submit], button:has-text("Sign In"), button:has-text("Log In")'
            await page.click(submit)
            await page.wait_for_timeout(4000)

            if 'mfa' in page.url.lower() or 'verify' in page.url.lower():
                raise ScraperError(
                    'UPS вимагає MFA. Вимкніть двофакторну аутентифікацію '
                    'для цього акаунту або використовуйте CSV-імпорт вручну.'
                )

        if 'billing.ups.com' in page.url:
            self.log.info('UPS login successful.')
            return True

        self.log.error('UPS login failed. URL: %s', page.url)
        return False

    async def download_invoices(self, page, start_date: str, end_date: str) -> list[str]:
        # TODO: реалізувати після тестування JLCPCB
        raise NotImplementedError(
            'UPS scraper ще в розробці. '
            'Використайте CSV-імпорт на сторінці UPS Billing.'
        )
