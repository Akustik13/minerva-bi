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
    'rag.datasheets':  Action('Пошук по всіх даташитах у RAG (помічник по продукції, право «📦»)', READ),
    'rag.history':     Action('Пошук в історії RAG: листи своїх скриньок і документи бази знань, лише перевірені '
                              'Minerva (право «🔎»)', READ),
    'product.propose': Action('Запропонувати поле чи параметр товару — застосовує людина (✓)', INTERNAL),
    'mail.propose':    Action('Підготувати лист на адресу з тексту завдання — надсилає людина', INTERNAL),
    'stock.receipt':   Action('Прихід на склад, новий товар — застосовує людина (✓)', INTERNAL),
    'calendar.event':  Action('Подія в календарі людини — застосовує людина (✓)', INTERNAL),
    'shop.listing':    Action('Показати / сховати в інтернет-магазині, ціна в магазині — лише з ✓ людини', EXTERNAL),
    'digikey.prices':  Action('Ціни на DigiKey за правилами Minerva, строк відвантаження — лише з ✓ людини', EXTERNAL),
    'order.read':      Action('Замовлення, що треба відправити, позиції й склад, перевізники', READ),
    'shipping.draft':  Action('Чернетка відправлення з замовлення — застосовує людина (✓)', INTERNAL),
    'order.update':    Action('Трек-номер, кур’єр, статус, строк відправки замовлення — застосовує людина (✓)',
                              INTERNAL),
    'purchase.read':   Action('Закупівлі в постачальників і їхні посилки', READ),
    'purchase.draft':  Action('Нова закупівля з позиціями й трек-номер вхідної посилки — застосовує людина (✓)',
                              INTERNAL),
    'offer.draft':     Action('Чернетка Angebot клієнту (ціни й строки — з Minerva) — застосовує людина (✓)',
                              INTERNAL),
    'shipping.book':   Action('Оформити відправлення в перевізника (етикетка, оплата)', NEVER),
    'product.price':   Action('Базові ціни й ціна продажу товару, списання й коригування залишків', NEVER),
}


def allowed(rule, key) -> bool:
    """Чи може агент за цим дорученням виконати дію key."""
    action = ACTIONS.get(key)
    if action is None or action.level == NEVER or not rule.enabled:
        return False
    if action.level == EXTERNAL:
        return rule.mode == rule.SEND
    return True
