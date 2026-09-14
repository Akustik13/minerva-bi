# Minerva Web Scraper

Модуль для автоматичного завантаження рахунків-фактур з постачальницьких порталів.
Використовує Playwright + реальний Chrome — імітує дії користувача в браузері.

## Навіщо

Деякі постачальники (JLCPCB, UPS) не мають API для завантаження інвойсів.
Scraper автоматично логіниться, знаходить рахунки за діапазоном дат і завантажує PDF.

## Технологія

- **Playwright** (`playwright>=1.40`) — браузерна автоматизація
- **`channel='chrome'`** — використовує системний Google Chrome (не Playwright Chromium)
- **Persistent context** — зберігає cookies між запусками; CAPTCHA розв'язується лише один раз
- **`accept_downloads=True`** — Playwright перехоплює завантаження без діалогів

## Встановлення

```powershell
pip install playwright
playwright install chrome
```

## Використання

```powershell
cd C:\tabele_mvp\scraper

# Завантажити рахунки JLCPCB за серпень–вересень 2026
python run.py jlcpcb `
  --username prym.via@gmail.com `
  --password YOURPASS `
  --output C:\tabele_mvp\downloads `
  --start 2026-08-01 `
  --end 2026-09-30
```

Файли зберігаються у `--output\jlcpcb\invoice_W<batch>.pdf`.

### Параметри

| Параметр | За замовчуванням | Опис |
|----------|-----------------|------|
| `site` | — | Сайт: `jlcpcb` або `ups` |
| `--username` | — | Email / логін |
| `--password` | — | Пароль |
| `--start` | 1 січня поточного року | Дата від (YYYY-MM-DD) |
| `--end` | Сьогодні | Дата до (YYYY-MM-DD) |
| `--output` | `./downloads` | Папка для PDF |
| `--no-headless` | — | Показати вікно браузера (для debugging) |
| `--slow` | — | Сповільнити дії на 500ms (для debugging) |

## Структура

```
scraper/
├── base.py          # BaseScraper — логін, persistent session, download
├── run.py           # CLI точка входу (argparse)
├── sites/
│   ├── jlcpcb.py   # JLCPCB invoice scraper ✅ робочий
│   └── ups.py      # UPS Billing scraper (stub, в розробці)
└── README.md
```

## JLCPCB — деталі

**Логін:** через `passport.jlcpcb.com` з OAuth redirect.
Якщо reCAPTCHA — вирішується вручну один раз, потім сесія зберігається.

**Billing page:** `jlcpcb.com/user-center/payments/billing`
Таблиця Batch Order # → для кожного рядка відкривається сторінка інвойсу.

**Завантаження:** клік кнопки `span:has-text("DOWNLOAD")` → Playwright перехоплює PDF.
Файл: `invoice_W<batch>.pdf` (напр. `invoice_W2026090920319128.pdf`).

**Дати:** рядки йдуть від новіших до старіших; скрипт зупиняється коли рядок виходить за `--start`.

## Сесія і CAPTCHA

Сесія зберігається у `<output>/sessions/jlcpcb/` (Chrome profile).

- **Перший запуск:** відкривається вікно Chrome, треба вирішити CAPTCHA і натиснути Sign In
- **Наступні запуски:** сесія автоматично відновлюється (логін не потрібен)
- **Якщо сесія закінчилась:** запусти з `--no-headless` щоб побачити браузер

Для скидання сесії (примусовий повторний логін):
```powershell
Remove-Item -Recurse -Force C:\tabele_mvp\downloads\sessions\jlcpcb
```

## Debugging

Debug-скріншоти зберігаються у `<output>/jlcpcb/_debug_jlcpcb_*.png` автоматично при помилках.

```powershell
# Показати браузер + сповільнити для ручного спостереження
python run.py jlcpcb --username ... --password ... --no-headless --slow
```

## Додавання нового сайту

1. Створити `sites/newsite.py` успадкувавши `BaseScraper`
2. Реалізувати `login(page)`, `download_invoices(page, start, end)`, `_is_logged_in(page)`
3. Зареєструвати в `SCRAPERS` у `run.py`

## Примітки

- Файли також можуть з'являтись у `C:\Users\{user}\Downloads\` — це нормальна поведінка Chrome.
  Наші PDF зберігаються у `--output\jlcpcb\` і є основними.
- `docker-compose down -v` — **ЗАБОРОНЕНО** (видаляє БД Minerva)
- Модуль не є частиною Django — запускається окремим Python-скриптом
