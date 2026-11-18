"""Меню «Дія» списків адмінки — групами за темою (optgroup), щоб потрібне знаходилось одразу.

class ProductAdmin(GroupedActionsMixin, admin.ModelAdmin):
    ACTION_GROUPS = [("📦 Товар", ["action_set_category", …]), ("🛒 DigiKey", [...])]

Дії поза групами (і «Видалити обрані») — в останній групі ACTION_REST. Порядок груп і дій усередині — як в
ACTION_GROUPS; дії, яких людині не видно (права), Django прибирає ще до групування, тож порожня група не
показується. Плоский список для панелі праворуч — `flat_choices` (core/panel_edit.admin_actions).
"""
from django.db.models import BLANK_CHOICE_DASH

ACTION_REST = "📋 Інше"


def flat_choices(choices) -> list[tuple]:
    """[(назва, підпис)] з вибору з групами або без них."""
    out = []
    for name, label in choices or []:
        if isinstance(label, (list, tuple)):
            out.extend((n, l) for n, l in label)
        else:
            out.append((name, label))
    return out


class GroupedActionsMixin:
    ACTION_GROUPS: list = []
    ACTION_REST = ACTION_REST

    def get_action_choices(self, request, default_choices=None):
        flat = super().get_action_choices(request, BLANK_CHOICE_DASH if default_choices is None else default_choices)
        if not self.ACTION_GROUPS:
            return flat
        blank = [c for c in flat if not c[0]]
        desc = {name: label for name, label in flat if name}
        grouped, used = list(blank), set()
        for title, names in self.ACTION_GROUPS:
            items = [(n, desc[n]) for n in names if n in desc]
            if items:
                grouped.append((title, items))
                used.update(n for n, _ in items)
        rest = [(n, d) for n, d in desc.items() if n not in used]
        if rest:
            grouped.append((self.ACTION_REST, rest))
        return grouped
