"""
python manage.py create_strategy_templates [--force]

Creates 4 built-in CRM workflow strategy templates.
With --force: re-creates existing templates (delete + create).
"""
from django.core.management.base import BaseCommand


TEMPLATES = [
    {
        "name": "Реактивація (At Risk / Lost)",
        "description": (
            "Стратегія для клієнтів із сегментами At Risk або Lost. "
            "Серія контактів з метою відновити взаємодію."
        ),
        "behavior_type": "reactivation",
        "steps": [
            {
                "order": 1, "step_type": "email",
                "title": "Email — повернення",
                "description": (
                    "Надіслати персоналізований email. "
                    "Нагадати про переваги, запропонувати знижку або бонус."
                ),
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 40,
            },
            {
                "order": 2, "step_type": "decision",
                "title": "Відповідь на email?",
                "description": "Чи відреагував клієнт на email (відкрив / відповів)?",
                "delay_days": 5,
                "canvas_x": 40, "canvas_y": 160,
            },
            {
                "order": 3, "step_type": "call",
                "title": "Дзвінок — особистий контакт",
                "description": (
                    "Зателефонувати. Запитати про потреби, "
                    "запропонувати допомогу або спеціальну пропозицію."
                ),
                "delay_days": 3,
                "canvas_x": 40, "canvas_y": 280,
            },
            {
                "order": 4, "step_type": "decision",
                "title": "Зацікавлений після дзвінка?",
                "description": "Чи виявив клієнт зацікавленість після дзвінка?",
                "delay_days": 1,
                "canvas_x": 40, "canvas_y": 400,
            },
            {
                "order": 5, "step_type": "pause",
                "title": "Пауза 90 днів",
                "description": "Призупинити активні контакти на 90 днів. Повернутися пізніше.",
                "delay_days": 90,
                "canvas_x": 40, "canvas_y": 520,
            },
        ],
        "branch_map": {
            # (from_order, direction) → to_order
            (2, "yes"): 3,
            (2, "no"):  5,
            (4, "yes"): None,  # strategy done
            (4, "no"):  5,
        },
    },
    {
        "name": "Нарощування (Promising)",
        "description": (
            "Стратегія для клієнтів із сегментом Promising. "
            "Серія утеплюючих контактів для переведення у Champion."
        ),
        "behavior_type": "nurturing",
        "steps": [
            {
                "order": 1, "step_type": "email",
                "title": "Email — знайомство з продуктом",
                "description": "Надіслати email з описом найкращих продуктів / кейсів.",
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 40,
            },
            {
                "order": 2, "step_type": "pause",
                "title": "Пауза 14 днів",
                "description": "Дати клієнту час ознайомитися з матеріалами.",
                "delay_days": 14,
                "canvas_x": 40, "canvas_y": 160,
            },
            {
                "order": 3, "step_type": "email",
                "title": "Email — кейс або відгук",
                "description": "Поділитися кейсом клієнта або відгуком, актуальним для сегменту.",
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 280,
            },
            {
                "order": 4, "step_type": "decision",
                "title": "Відповідь / взаємодія?",
                "description": "Чи натиснув клієнт на посилання або відповів на email?",
                "delay_days": 7,
                "canvas_x": 40, "canvas_y": 400,
            },
            {
                "order": 5, "step_type": "pause",
                "title": "Пауза 60 днів",
                "description": "Пауза перед наступним циклом нарощування.",
                "delay_days": 60,
                "canvas_x": 40, "canvas_y": 520,
            },
        ],
        "branch_map": {
            (4, "yes"): None,  # done_pos → strategy complete (Champion!)
            (4, "no"):  5,
        },
    },
    {
        "name": "Утримання VIP (Champion)",
        "description": (
            "Стратегія для VIP-клієнтів із сегментом Champion. "
            "Регулярна подяка і особистий контакт для утримання лояльності."
        ),
        "behavior_type": "retention",
        "steps": [
            {
                "order": 1, "step_type": "email",
                "title": "Email — подяка VIP",
                "description": (
                    "Надіслати персональний лист подяки. "
                    "Підкреслити цінність клієнта, надати ексклюзивну інформацію."
                ),
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 40,
            },
            {
                "order": 2, "step_type": "pause",
                "title": "Пауза 30 днів",
                "description": "Дати час після email перед дзвінком.",
                "delay_days": 30,
                "canvas_x": 40, "canvas_y": 160,
            },
            {
                "order": 3, "step_type": "call",
                "title": "Дзвінок — зворотній зв'язок",
                "description": (
                    "Подзвонити, запитати про досвід роботи, "
                    "дізнатися про нові потреби."
                ),
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 280,
            },
            {
                "order": 4, "step_type": "email",
                "title": "Лог — підсумок взаємодії",
                "description": "Записати результат дзвінка, зафіксувати потреби.",
                "delay_days": 1,
                "canvas_x": 40, "canvas_y": 400,
            },
            {
                "order": 5, "step_type": "pause",
                "title": "Пауза 60 днів",
                "description": "Наступний цикл VIP-утримання через 60 днів.",
                "delay_days": 60,
                "canvas_x": 40, "canvas_y": 520,
            },
        ],
        "branch_map": {},
    },
    {
        "name": "Онбординг (нові клієнти)",
        "description": (
            "Стратегія для нових клієнтів. "
            "Серія вітальних контактів для швидкого залучення."
        ),
        "behavior_type": "onboarding",
        "steps": [
            {
                "order": 1, "step_type": "email",
                "title": "Email — вітання",
                "description": (
                    "Надіслати вітальний email. "
                    "Представитися, описати процес роботи, надати контакти."
                ),
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 40,
            },
            {
                "order": 2, "step_type": "pause",
                "title": "Пауза 3 дні",
                "description": "Дати час ознайомитися з вітальним листом.",
                "delay_days": 3,
                "canvas_x": 40, "canvas_y": 160,
            },
            {
                "order": 3, "step_type": "email",
                "title": "Email — корисні ресурси",
                "description": "Надіслати FAQ, корисні посилання, відповіді на часті запитання.",
                "delay_days": 0,
                "canvas_x": 40, "canvas_y": 280,
            },
            {
                "order": 4, "step_type": "decision",
                "title": "Чи задав клієнт питання?",
                "description": "Чи відповів або написав клієнт після другого email?",
                "delay_days": 7,
                "canvas_x": 40, "canvas_y": 400,
            },
            {
                "order": 5, "step_type": "call",
                "title": "Лог — завершення онбордингу",
                "description": "Зафіксувати успішне завершення онбордингу.",
                "delay_days": 1,
                "canvas_x": 40, "canvas_y": 520,
            },
        ],
        "branch_map": {
            (4, "yes"): 5,
            (4, "no"):  5,
        },
    },
]


class Command(BaseCommand):
    help = "Create 4 built-in CRM strategy templates"

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Delete and re-create existing templates",
        )

    def handle(self, *args, **options):
        from strategy.models import StrategyTemplate, TemplateStep

        force = options["force"]
        created_count = 0
        skipped_count = 0

        for tdata in TEMPLATES:
            name = tdata["name"]
            if StrategyTemplate.objects.filter(name=name).exists():
                if force:
                    StrategyTemplate.objects.filter(name=name).delete()
                    self.stdout.write(f"  🗑  Видалено: {name}")
                else:
                    self.stdout.write(f"  ⏭  Пропущено (вже є): {name}")
                    skipped_count += 1
                    continue

            template = StrategyTemplate.objects.create(
                name=name,
                description=tdata["description"],
                behavior_type=tdata["behavior_type"],
                is_active=True,
            )

            step_objs = {}
            for sdata in tdata["steps"]:
                step = TemplateStep.objects.create(
                    template=template,
                    order=sdata["order"],
                    step_type=sdata["step_type"],
                    title=sdata["title"],
                    description=sdata["description"],
                    delay_days=sdata["delay_days"],
                    canvas_x=sdata.get("canvas_x", 0),
                    canvas_y=sdata.get("canvas_y", 0),
                )
                step_objs[sdata["order"]] = step

            # Wire up decision branches
            for (from_order, direction), to_order in tdata.get("branch_map", {}).items():
                from_step = step_objs.get(from_order)
                to_step   = step_objs.get(to_order) if to_order else None
                if from_step:
                    if direction == "yes":
                        from_step.next_step_yes = to_step
                    else:
                        from_step.next_step_no = to_step
                    from_step.save(update_fields=[f"next_step_{direction}"])

            self.stdout.write(self.style.SUCCESS(
                f"  ✅ Створено: {name} ({len(tdata['steps'])} кроків)"
            ))
            created_count += 1

        self.stdout.write(
            f"\nГотово: {created_count} створено, {skipped_count} пропущено.\n"
        )
