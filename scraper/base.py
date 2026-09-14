"""
scraper/base.py — BaseScraper з persistent browser session.

Windows: реальний Chrome (channel='chrome') — краще проходить reCAPTCHA.
Linux/Docker: Playwright Chromium + Xvfb virtual display — керується через noVNC.

Сесія зберігається в:
  - Windows:      <output_dir>/sessions/<site_name>/
  - Docker/Linux: $SCRAPER_SESSION_DIR/<site_name>/  (env var)
"""
import asyncio
import logging
import os
import platform
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)

IS_LINUX = platform.system() == 'Linux'

_ANTI_BOT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins',   {get: () => [1, 2, 3]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = {runtime: {}};
"""

_LINUX_ARGS = [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--disable-blink-features=AutomationControlled',
    '--disable-gpu',
]

_WIN_ARGS = [
    '--disable-blink-features=AutomationControlled',
]


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
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.headless   = headless
        self.slow_mo    = slow_mo
        self.log        = logging.getLogger(f'scraper.{self.SITE_NAME}')

        # Сесія: Docker → /sessions/<site>/, Windows → output/sessions/<site>/
        session_base = os.environ.get('SCRAPER_SESSION_DIR', '')
        if session_base:
            self.session_dir = Path(session_base) / self.SITE_NAME
        else:
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
    async def login(self, page) -> bool: ...

    @abstractmethod
    async def download_invoices(self, page, start_date: str, end_date: str) -> list[str]: ...

    async def _is_logged_in(self, page) -> bool:
        return False

    # ── Orchestration ─────────────────────────────────────────────────────────

    async def run_async(self, start_date: str, end_date: str) -> list[str]:
        from playwright.async_api import async_playwright

        self.log.info('Platform: %s | Output: %s', platform.system(), self.output_dir)
        self.log.info('Session dir: %s', self.session_dir)

        # Видаляємо stale lock-файли Chromium (залишаються після рестарту контейнера).
        # SingletonLock — це symlink, тому перевіряємо is_symlink() а не exists().
        for lock_name in ('SingletonLock', 'SingletonCookie', 'SingletonSocket'):
            lock_file = self.session_dir / lock_name
            if lock_file.is_symlink() or lock_file.exists():
                try:
                    lock_file.unlink()
                    self.log.info('Removed stale lock: %s', lock_name)
                except Exception:
                    pass

        async with async_playwright() as p:
            if IS_LINUX:
                # Docker/NAS — Playwright Chromium з Xvfb virtual display
                # headless=False — браузер видимий через VNC/noVNC
                self.log.info('Linux mode — Playwright Chromium + Xvfb')
                ctx = await p.chromium.launch_persistent_context(
                    str(self.session_dir),
                    headless=False,
                    slow_mo=self.slow_mo,
                    args=_LINUX_ARGS,
                    locale='en-US',
                    timezone_id='Europe/Berlin',
                    viewport={'width': 1440, 'height': 900},
                    accept_downloads=True,
                )
            else:
                # Windows — реальний Chrome (краще проходить reCAPTCHA)
                self.log.info('Windows mode — real Chrome (channel=chrome)')
                ctx = await p.chromium.launch_persistent_context(
                    str(self.session_dir),
                    channel='chrome',
                    headless=False,
                    slow_mo=self.slow_mo,
                    args=_WIN_ARGS,
                    locale='en-US',
                    timezone_id='Europe/Berlin',
                    viewport={'width': 1440, 'height': 900},
                    accept_downloads=True,
                )

            await ctx.add_init_script(_ANTI_BOT_SCRIPT)
            page = await ctx.new_page()

            try:
                if await self._is_logged_in(page):
                    self.log.info('Valid session — skipping login!')
                else:
                    if IS_LINUX:
                        self.log.info(
                            'Session expired. Open noVNC at %s to solve CAPTCHA.',
                            os.environ.get('SCRAPER_VNC_URL', 'http://NAS_IP:6080'),
                        )
                    ok = await self.login(page)
                    if not ok:
                        await self._screenshot(page, 'login_failed')
                        raise ScraperError(f'Login failed for {self.SITE_NAME}.')

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
        return asyncio.run(self.run_async(start_date, end_date))
