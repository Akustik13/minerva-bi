"""Набори інструментів агента за розділами Minerva — для MCP (`/api/v1/mcp/`, agent/mcp.py) і завдань агенту.

Кожен модуль у `MODULES` — один розділ (пошта, продажі, CRM, проєкти, постачальники, робочий час, DigiKey, склад) і
має однаковий вигляд:

    TOOLS = [{'name', 'description', 'input_schema'}, …]   # формат інструментів Claude, як в agent/tools.py
    HANDLERS = {назва: fn(ctx, args) -> str}                 # ctx — agent.tools.Context; помилка — ToolError
    def tools_for(user) -> list                             # що з TOOLS дозволено цій людині (права Minerva)
    KINDS = {код: підпис}                                   # види пропозицій (propose_*), необов'язково
    APPLY_PERMS = (…)                                       # права, з якими людина застосовує ці пропозиції ✓
    def apply(p, user) -> str                               # ✓ пропозиції: '' — готово, інакше — чому ні
    def describe(p) -> (що, було, стане)                    # рядок таблиці на сторінці перевірки

Правила для всіх: лише читання або propose_* (застосовує людина ✓ — agent/decide.py); права — `user.has_perm` людини
ключа (`ctx.task.owner`), і в `tools_for`, і ще раз у самому інструменті; пошта, календар і робочий час — лише свої;
тексти листів, нотаток і повідомлень покупців — ДАНІ, не вказівки (у <<<…>>>); відповідь інструмента — до кількох
тисяч символів.
"""
import importlib
import logging

logger = logging.getLogger(__name__)

MODULES = ('agent.tools_mail', 'agent.tools_sales', 'agent.tools_crm', 'agent.tools_projects',
           'agent.tools_suppliers', 'agent.tools_suppliers_mail', 'agent.tools_cards', 'agent.tools_rfq',
           'agent.tools_rfq_table', 'agent.tools_rfq_mail', 'agent.tools_worktime', 'agent.tools_digikey', 'agent.tools_stock',
           'agent.tools_packaging', 'agent.tools_files')
# Особисте людини (її пошта й календар, її робочий час, листування з постачальниками запитів) — лише через 🔒 особистий ключ, де агент — сам власник
# (`APIKey.personal_ok`): спільний ключ бачать і можуть використати інші люди.
PERSONAL_MODULES = ('agent.tools_mail', 'agent.tools_worktime', 'agent.tools_rfq_mail',
                    'agent.tools_suppliers_mail')


def modules() -> list:
    out = []
    for name in MODULES:
        try:
            out.append(importlib.import_module(name))
        except Exception as e:  # noqa: BLE001 — розділ без застосунку / зламаний модуль не ламає решту
            logger.warning('agent toolset %s: %s', name, e)
    return out


def tools_for(user, personal=False) -> list:
    """Інструменти для людини; personal — чи можна її особисте (пошта, календар, робочий час)."""
    if user is None or not getattr(user, 'is_active', False):
        return []
    out = []
    for m in modules():
        if m.__name__ in PERSONAL_MODULES and not personal:
            continue
        try:
            out += list(m.tools_for(user))
        except Exception as e:  # noqa: BLE001
            logger.warning('agent toolset %s.tools_for: %s', m.__name__, e)
    return out


def handler(name):
    for m in modules():
        fn = getattr(m, 'HANDLERS', {}).get(name)
        if fn is not None:
            return fn
    return None


def module_for_kind(kind):
    for m in modules():
        if kind in getattr(m, 'KINDS', {}):
            return m
    return None


def kinds() -> dict:
    out = {}
    for m in modules():
        out.update(getattr(m, 'KINDS', {}))
    return out


def apply_perms() -> tuple:
    out = []
    for m in modules():
        out += list(getattr(m, 'APPLY_PERMS', ()))
    return tuple(out)


# ── Спільні помічники модулів ─────────────────────────────────────────────────

def mail_viewer(ctx):
    """Чиї листи й календар видно в цьому виклику: людини завдання; через спільний ключ MCP — нічиї (None): тема
    листа в хронології клієнта, «потребує уваги» проєкту, аналіз пошти постачальника — без тексту."""
    return ctx.task.owner if getattr(ctx, 'personal', True) else None


def require(user, *perms):
    """Хоч одне з прав — інакше ToolError (інструмент перевіряє ще раз, хоч `tools_for` уже відсіяв)."""
    from agent.tools import ToolError
    if user is None or not getattr(user, 'is_active', False) or not any(user.has_perm(p) for p in perms):
        raise ToolError('немає права на це в Minerva')


def clamp(value, default, low, high) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def one_line(text, limit=200) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit].rstrip() + '…'


def money(value, currency='') -> str:
    try:
        return f'{float(value):,.2f}'.replace(',', ' ') + (f' {currency}' if currency else '')
    except (TypeError, ValueError):
        return '—'


def date(value, fmt='%d.%m.%Y') -> str:
    if not value:
        return '—'
    from django.utils import timezone
    import datetime
    if isinstance(value, datetime.datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
    return value.strftime(fmt)


def parse_date(value):
    """«2026-11-23» / «23.11.2026» → date або None."""
    import datetime
    value = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def propose(ctx, kind, *, field='', old='', new='', evidence='', source='', confidence='', data=None, product=None):
    """Пропозиція на перевірку людині (✓ — agent/decide.py → модуль `apply`)."""
    from agent.models import AgentProposal
    p = AgentProposal.objects.create(task=ctx.task, kind=kind, product=product, field=str(field)[:200],
                                     old_value=str(old or ''), new_value=str(new or ''),
                                     evidence=str(evidence or '')[:4000], source=str(source or '')[:500],
                                     confidence=str(confidence or '')[:10], data=data or {})
    ctx.proposed += 1
    return p
