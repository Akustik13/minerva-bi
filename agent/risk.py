"""Рівні ризику дій агента — що він може зробити сам, а що лише з людиною.

• read — лише читає (листи скриньки, склад, замовлення) — завжди;
• internal — зміна всередині Minerva, яку легко скасувати (чернетка листа, поле товару) — за дорученням сам;
• external — назовні або незворотне (надіслати лист, ціна в магазині чи на DigiKey, відправлення, askDANTE) —
  лише якщо людина в дорученні прямо дозволила (режим «надсилати самому»), з межами;
• never — ключі, паролі, оплати, видалення — агенту недоступне.
Нова дія агента — новий рядок тут; `allowed` вирішує за дорученням.
"""
from dataclasses import dataclass

READ, INTERNAL, EXTERNAL, NEVER = 'read', 'internal', 'external', 'never'
LEVEL_TEXT = {READ: 'читає', INTERNAL: 'змінює в Minerva', EXTERNAL: 'назовні — з вашого дозволу', NEVER: 'ніколи'}


@dataclass(frozen=True)
class Action:
    label: str
    level: str


ACTIONS = {
    'mail.read':  Action('Прочитати лист і гілку своєї скриньки', READ),
    'mail.facts': Action('Факти з Minerva про лист: замовлення, відправлення, склад, клієнт', READ),
    'mail.draft': Action('Зберегти чернетку відповіді в «📝 Чернетках»', INTERNAL),
    'mail.send':  Action('Надіслати відповідь співрозмовнику гілки (через кілька хвилин, можна скасувати)', EXTERNAL),
    # завдання (agent/tasks.py, agent/tools.py): агент лише читає й пропонує, застосовує людина (agent/decide.py)
    'product.read':    Action('Картка товару, пошук товарів', READ),
    'datasheet.read':  Action('Текст даташита товару', READ),
    'web.search':      Action('Пошук в інтернеті (якщо дозволено в завданні)', READ),
    'product.propose': Action('Запропонувати поле чи параметр товару — застосовує людина (✓)', INTERNAL),
    'mail.propose':    Action('Підготувати лист на адресу з тексту завдання — надсилає людина', INTERNAL),
    'product.price':   Action('Ціни, залишки, видимість у магазині й на DigiKey', NEVER),
}


def allowed(rule, key) -> bool:
    """Чи може агент за цим дорученням виконати дію key."""
    action = ACTIONS.get(key)
    if action is None or action.level == NEVER or not rule.enabled:
        return False
    if action.level == EXTERNAL:
        return rule.mode == rule.SEND
    return True
