"""🧩 «Це може зробити агент» — підказка у віконці «Minerva» (rag_assistant) під повідомленням людини.

Без AI, за словами: прохання ЗРОБИТИ роботу (знайди / внеси / додай / підготуй / надішли / постав …) над товарами,
даташитами, складом, листом, відправленнями, магазином, DigiKey чи календарем → кнопка «🧩 Підготувати завдання
агенту»: форма нового завдання (`/admin/agent/agenttask/add/?instruction=…&ids=…`) з цим текстом і товарами, чиї
артикули є в тексті; запускає людина кнопкою «Зберегти». «Відповідай на такі листи …» → «🤖 Доручення агента»
(правий клік на листі в пошті). Питання «як / де / чи …» — не завдання: на них відповідає гід. Кнопку бачить лише
людина з правом давати завдання / доручення (`agent.add_agenttask` / `agent.add_agentrule`).
"""
import re
from urllib.parse import urlencode

INSTRUCTION_MAX = 3000
PRODUCTS_MAX = 200

_ASK_RE = re.compile(r'^\W*(як|де|чи|що|навіщо|нащо|чому|коли|скільки|який|яка|яке|які|how|where|what|why|wie|wo|'
                     r'was|warum)(?!\w)', re.I)
_HOWTO_RE = re.compile(r'(?<!\w)(як|де|how|where)\s+(\w+\s+){0,2}(знайти|внести|додати|заповнити|оновити|перенести|'
                       r'підготувати|надіслати|поставити|find|add)(?!\w)', re.I)
_DO_RE = re.compile(
    r'(?<!\w)(знайди|знайдіть|знайти|внеси|внесіть|внести|додай|додайте|додати|заповни|заповніть|заповнити|онови|'
    r'оновіть|оновити|перенеси|перенесіть|перенести|перевір|перевірте|прочитай|прочитайте|перебери|переберіть|'
    r'витягни|підготуй|підготуйте|підготувати|надішли|надішліть|надіслати|відправ|відправте|постав|поставте|'
    r'поставити|сховай|сховайте|створи|створіть|оприбуткуй|оприбуткуйте|'
    r'найди|добавь|заполни|обнови|подготовь|отправь)(?!\w)', re.I)
_OBJECT_RE = re.compile(
    r'(даташит|datasheet|datenblatt|параметр|характеристик|ваг[аиуі]|\bhs\b|країн|склад|товар|артикул|лист|e-?mail|'
    r'імейл|lieferschein|накладн|прихід|відправлен|замовлен|трек|магазин|\bshop|digikey|календар|зустріч|дедлайн|'
    r'ціни|ціну|строк)', re.I)
_RULE_RE = re.compile(r'(на (такі|ці|подібні|всі|усі) лист|на листи (від|з|типу|про)|завжди відповідай|'
                      r'відповідай (сам|сама|автоматично)|автовідповід|відповідати (на такі|самому|самій|автоматично))',
                      re.I)
_SKU_RE = re.compile(r'(?<![\w./-])([A-Za-z0-9][A-Za-z0-9._/-]{2,62}[A-Za-z0-9])(?![\w-])')


def is_rule(text) -> bool:
    return bool(_RULE_RE.search(str(text or '')))


def is_task(text) -> bool:
    """Прохання зробити роботу, а не запитання «як …»."""
    text = str(text or '').strip()
    if not text or _ASK_RE.match(text) or _HOWTO_RE.search(text):
        return False
    return bool(_DO_RE.search(text) and _OBJECT_RE.search(text))


def product_ids(text) -> list:
    """Товари, чиї артикули (з цифрами й літерами) є в тексті — за порядком згадки, без повторів."""
    from inventory.models import Product
    from inventory.services.stock import resolve_sku
    out = []
    for token in _SKU_RE.findall(str(text or '')):
        if not (any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)):
            continue
        product = Product.objects.filter(sku__iexact=token).only('pk').first() or resolve_sku(token)
        if product is not None and product.pk not in out:
            out.append(product.pk)
        if len(out) >= PRODUCTS_MAX:
            break
    return out


def suggest(text, user) -> dict | None:
    """{kind task | rule, title, text, label, url} або None — що показати під повідомленням людини."""
    text = str(text or '').strip()
    if not text or user is None or not getattr(user, 'is_active', False):
        return None
    if is_rule(text):
        if not user.has_perm('agent.add_agentrule'):
            return None
        return {'kind': 'rule', 'title': '🤖 Це — доручення агентові в пошті',
                'text': 'Відкрийте лист такого типу в пошті → правий клік → «🤖 Доручити агенту: відповідати на такі '
                        'листи…»: умови, як відповідати, «🧪 Спробувати» покаже відповідь на цей лист. Спершу агент '
                        'готує чернетки — надсилаєте ви.',
                'label': '📥 Відкрити пошту', 'url': '/email/'}
    if not is_task(text) or not user.has_perm('agent.add_agenttask'):
        return None
    from django.urls import reverse
    ids = product_ids(text)
    query = {'instruction': text[:INSTRUCTION_MAX]}
    if ids:
        query['ids'] = ','.join(str(pk) for pk in ids)
    found = f', вибрано товарів з артикулів у тексті: {len(ids)}' if ids else ''
    return {'kind': 'task', 'title': '🧩 Це може зробити агент Minerva',
            'text': 'Він прочитає картки товарів, даташити чи лист і запропонує зміни таблицею «було → стане» — '
                    f'застосовуєте ви ✓. Форма завдання вже заповнена вашим текстом{found}. Перевірте й натисніть '
                    '«Зберегти» — агент почне працювати, а коли закінчить, повідомить вас.',
            'label': '🧩 Підготувати завдання агенту', 'url': reverse('admin:agent_agenttask_add') + '?' + urlencode(query)}
