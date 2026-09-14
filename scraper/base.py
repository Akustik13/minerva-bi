"""
scraper/base.py — BaseScraper з persistent browser session.

Сесія зберігається в <output_dir>/sessions/<site_name>/
Перший запуск: логін вручну (CAPTCHA один раз).
Наступні запуски: сесія завантажується — логін не потрібен.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

_CHROME_ARGS = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-blink-features=AutomationControlled',
    '--disable-infobars',
]

_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

_ANTI_BOT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
"""


class ScraperError(Exception):
    pass


class BaseScraper(ABC):
    SITE_NAME: str = 'base'

    def __init__(self, username: str, password: str,
                 output_dir: str = './downloads',
                 headless: bool = True,
                 slow_mo: int = 0):
        self.username   = username
        self.password   = password
        # resolve() → завжди абсолютний шлях незалежно від CWD
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.headless   = headless
        self.slow_mo    = slow_mo
        self.log        = logging.getLogger(f'scraper.{self.SITE_NAME}')

        # Сесія зберігається поруч з downloads/
        self.session_dir = self.output_dir / 'sessions' / self.SITE_NAME
        self.session_dir.mkdir(parents=True, exist_ok=True)

    # ── Screenshot ────────────────────────────────────────────────────────────

    async def _screenshot(self, page, name: str):
        path = self.output_dir / f'_debug_{self.SITE_NAME}_{name}.png'
        try:
            await page.screenshot(path=str(path), full_page=True)
            self.log.info('Screenshot saved: %s', path)
        except Exception:
            pass

    # ── Абстрактні методи ────────────────────────────────────────────────────

    @abstractmethod
    async def login(self, page) -> bool:
        """Логін на сайт. Повертає True якщо успішно."""
        ...

    @abstractmethod
    async def download_invoices(self, page, start_date: str, end_date: str) -> list[str]:
        """Завантажує рахунки. Повертає список шляхів до файлів."""
        ...

    async def _is_logged_in(self, page) -> bool:
        """
        Перевіряє чи є збережена сесія.
        Перевизначити в підкласі для точнішої перевірки.
        """
        return False

    # ── Orchestration з persistent context ───────────────────────────────────

    async def run_async(self, start_date: str, end_date: str) -> list[str]:
        from playwright.async_api import async_playwright

        self.log.info('Output dir: %s', self.output_dir)
        self.log.info('Session dir: %s', self.session_dir)

        async with async_playwright() as p:
            # Persistent context — зберігає cookies/localStorage між запусками.
            # channel='chrome' — використовує встановлений Chrome (не Playwright Chromium).
            # Реальний Chrome проходить reCAPTCHA без нескінченних задач з картинками.
            ctx = await p.chromium.launch_persistent_context(
                str(self.session_dir),
                channel='chrome',
                headless=False,   # Chrome з channel не підтримує headless для reCAPTCHA
                slow_mo=self.slow_mo,
                args=['--disable-blink-features=AutomationControlled'],
                locale='en-US',
                timezone_id='Europe/Berlin',
                viewport={'width': 1440, 'height': 900},
                accept_downloads=True,
            )
            await ctx.add_init_script(_ANTI_BOT_SCRIPT)
            page = await ctx.new_page()

            try:
                # Перевіряємо чи вже залогінені (збережена сесія)
                if await self._is_logged_in(page):
                    self.log.info('Valid session found — skipping login!')
                else:
                    self.log.info('No session or expired — starting login...')
                    ok = await self.login(page)
                    if not ok:
                        await self._screenshot(page, 'login_failed')
                        raise ScraperError(
                            f'Login failed for {self.SITE_NAME}. '
                            'See debug screenshot in downloads/'
                        )

                files = await self.download_invoices(page, start_date, end_date)
                self.log.info('Done. Downloaded %d file(s).', len(files))
                return files

            except ScraperError:
                raise
            except Exception as exc:
                await self._screenshot(page, 'error')
                raise ScraperError(f'{self.SITE_NAME} error: {exc}') from exc
            finally:
                await ctx.close()

    def run(self, start_date: str, end_date: str) -> list[str]:
        """Sync-обгортка для CLI."""
        return asyncio.run(self.run_async(start_date, end_date))
