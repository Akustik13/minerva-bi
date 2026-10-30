"""Переклади інтерфейсу (core/ui_text) з консолі.

    python manage.py ui_texts collect                      — каталог ← код
    python manage.py ui_texts estimate [--model …]         — скільки фраз без перекладу й скільки коштуватиме
    python manage.py ui_texts translate --yes [--model …] [--limit N]   — перекласти (рахунок API Anthropic)
"""
from django.core.management.base import BaseCommand, CommandError

from core.ui_text import catalog, translate


class Command(BaseCommand):
    help = "Каталог написів інтерфейсу й AI-переклад (en, de)"

    def add_arguments(self, parser):
        parser.add_argument("what", choices=["collect", "estimate", "translate"])
        parser.add_argument("--model", default=translate.DEFAULT_MODEL, choices=list(translate.MODELS))
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument("--yes", action="store_true", help="так, перекладати (це коштує грошей)")

    def handle(self, what, model, limit, yes, **opts):
        if what == "collect":
            res = catalog.sync()
            self.stdout.write(f"Написів: {res['total']} · нових: {res['new']} · зникло: {res['gone']}")
            return
        rows, langs = translate.todo()
        if limit:
            rows = rows[:limit]
        est = translate.estimate(rows, langs, model)
        self.stdout.write(f"Без перекладу: {est['phrases']} → {', '.join(langs)} · запитів {est['batches']} · "
                          f"≈ {est['tokens_in']} / {est['tokens_out']} токенів · ≈ ${est['usd']} ({model})")
        if what == "estimate":
            return
        if not yes:
            raise CommandError("Додайте --yes, щоб перекласти (гроші спишуться з рахунку API Anthropic).")
        res = translate.run(model=model, limit=limit, who="manage.py")
        self.stdout.write(f"{res.get('status')}: збережено {res.get('saved', 0)}, відхилено {res.get('rejected', 0)}, "
                          f"помилок {res.get('errors', 0)}, ≈ ${res.get('usd', 0)} {res.get('problem', '')}")
