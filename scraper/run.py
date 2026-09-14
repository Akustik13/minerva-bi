#!/usr/bin/env python3
"""
Minerva Web Scraper — CLI entry point.

Використання:
  python run.py jlcpcb --username EMAIL --password PASS [options]
  python run.py ups    --username EMAIL --password PASS [options]

Опції:
  --start     YYYY-MM-DD  Дата від (default: поточний рік)
  --end       YYYY-MM-DD  Дата до   (default: сьогодні)
  --output    PATH        Папка для файлів (default: ./downloads)
  --no-headless           Показати браузер (для debugging)
  --slow                  Сповільнити дії на 500ms (для debugging)
  --list                  Показати доступні скрейпери

Приклади:
  python run.py jlcpcb --username me@email.com --password mypass
  python run.py jlcpcb --username me@email.com --password mypass --no-headless --slow
  python run.py jlcpcb --username me@email.com --password mypass --start 2025-01-01 --end 2025-06-30
"""
import argparse
import logging
import os
import sys
from datetime import date, timedelta

# UTF-8 вивід на Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Додаємо scraper/ до шляху щоб sites/ могли імпортувати base
sys.path.insert(0, os.path.dirname(__file__))

# Налаштовуємо логи в консоль
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s  %(message)s',
    datefmt='%H:%M:%S',
)

SCRAPERS = {
    'jlcpcb': ('sites.jlcpcb', 'JLCScraper'),
    'ups':    ('sites.ups',    'UPSScraper'),
}


def _import_scraper(name: str):
    module_path, class_name = SCRAPERS[name]
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


def _default_start() -> str:
    return date(date.today().year, 1, 1).strftime('%Y-%m-%d')


def _default_end() -> str:
    return date.today().strftime('%Y-%m-%d')


def main():
    parser = argparse.ArgumentParser(
        description='Minerva Web Scraper — завантаження рахунків з сайтів постачальників',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('site', nargs='?', choices=list(SCRAPERS), help='Сайт для скрейпінгу')
    parser.add_argument('--username', '-u', help='Email / логін')
    parser.add_argument('--password', '-p', help='Пароль')
    parser.add_argument('--start',  default=_default_start(), help='Дата від YYYY-MM-DD')
    parser.add_argument('--end',    default=_default_end(),   help='Дата до YYYY-MM-DD')
    parser.add_argument('--output', default='./downloads',    help='Папка для файлів')
    parser.add_argument('--no-headless', dest='headless', action='store_false',
                        help='Показати браузер (для debugging)')
    parser.add_argument('--slow', dest='slow_mo', action='store_const', const=500, default=0,
                        help='Сповільнити дії (для debugging)')
    parser.add_argument('--list', action='store_true', help='Список доступних скрейперів')
    parser.set_defaults(headless=True)

    args = parser.parse_args()

    if args.list:
        print('Доступні скрейпери:')
        for name in SCRAPERS:
            print(f'  {name}')
        return

    if not args.site:
        parser.print_help()
        sys.exit(1)

    if not args.username or not args.password:
        parser.error('--username та --password обов\'язкові')

    # Перевіряємо playwright
    try:
        import playwright  # noqa: F401
    except ImportError:
        print(
            '\n❌ Playwright не встановлений.\n'
            'Виконайте:\n'
            '  pip install playwright\n'
            '  playwright install chromium\n'
        )
        sys.exit(1)

    print(f'\n🔍 Scraper: {args.site}')
    print(f'📅 Діапазон: {args.start} → {args.end}')
    print(f'📁 Папка:    {args.output}')
    print(f'🖥  Headless: {args.headless}')
    print()

    cls = _import_scraper(args.site)
    scraper = cls(
        username=args.username,
        password=args.password,
        output_dir=args.output,
        headless=args.headless,
        slow_mo=args.slow_mo,
    )

    try:
        files = scraper.run(args.start, args.end)
        if files:
            print(f'\n✅ Завантажено {len(files)} файл(ів):')
            for f in files:
                print(f'   {f}')
        else:
            print('\n⚠️  Файли не знайдено. Перевір:')
            print('   1. Правильні email/пароль?')
            print('   2. Є рахунки за вказаний діапазон дат?')
            print('   3. Запусти з --no-headless щоб побачити браузер')
            print('   4. Перевір debug-скріншот у папці downloads/')
    except Exception as e:
        print(f'\n❌ Помилка: {e}')
        print('   Запусти з --no-headless --slow для debugging')
        sys.exit(1)


if __name__ == '__main__':
    main()
