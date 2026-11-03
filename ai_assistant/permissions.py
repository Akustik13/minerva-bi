from core.models import UserProfile

ROLE_TOOLS = {
    'viewer': [
        'get_system_overview',
        'get_inventory_status',
    ],
    'staff': [
        'get_system_overview',
        'get_inventory_status',
        'get_recent_orders',
        'get_customer_info',
        'get_sales_analytics',
    ],
    'manager': [
        'get_system_overview',
        'get_inventory_status',
        'get_recent_orders',
        'get_customer_info',
        'get_sales_analytics',
        'get_financial_overview',
        'compose_email',
        'get_email_strategy',
    ],
    'admin': [
        'get_system_overview',
        'get_inventory_status',
        'get_recent_orders',
        'get_customer_info',
        'get_sales_analytics',
        'get_financial_overview',
        'compose_email',
        'get_email_strategy',
        'send_email',
        'create_order',
        'update_inventory',
        'get_audit_log',
    ],
}


def get_profile_for_telegram(telegram_id: int):
    """Знайти UserProfile по telegram_id."""
    try:
        return UserProfile.objects.select_related('user').get(telegram_id=telegram_id)
    except UserProfile.DoesNotExist:
        return None


# Що вміє кожен інструмент — людською мовою (вікно «Що я вмію» в чаті 🏛️, підказка моделі, гід)
TOOL_LABELS = {
    'get_system_overview':    '📊 Загальна картина: замовлення, виручка, клієнти',
    'get_inventory_status':   '📦 Склад: залишки, що закінчується',
    'get_recent_orders':      '🧾 Замовлення: останні, невідправлені, прострочені',
    'get_customer_info':      '👤 Клієнти: картка, сегмент',
    'get_sales_analytics':    '📈 Продажі й аналітика за період',
    'get_financial_overview': '💶 Фінансовий огляд: прострочене, дедлайни',
    'compose_email':          '✉️ Скласти лист клієнту — відкриєте в пошті й надішлете самі',
    'get_email_strategy':     '🎯 Стратегія листування з клієнтом',
    'send_email':             '📤 Надіслати лист — лише після вашого «так» у чаті',
    'create_order':           '➕ Створити замовлення — лише після вашого «так»',
    'update_inventory':       '✏️ Змінити залишок — лише після вашого «так»',
    'get_audit_log':          '🕘 Журнал дій',
}
# інструмент → галочка «🔒 Дозволи» в «⚙️ Налаштування AI», що вимикає його для всіх
GLOBAL_SWITCHES = {
    'compose_email': ('ai_allow_email_sending', 'Дозволити AI відправляти email'),
    'send_email': ('ai_allow_email_sending', 'Дозволити AI відправляти email'),
    'get_email_strategy': ('ai_allow_email_sending', 'Дозволити AI відправляти email'),
    'create_order': ('ai_allow_order_creation', 'Дозволити AI створювати замовлення'),
    'update_inventory': ('ai_allow_inventory_edit', 'Дозволити AI редагувати склад'),
    'get_financial_overview': ('ai_allow_financial_data', 'Дозволити AI бачити фінансові дані'),
}


def effective_role(profile) -> str:
    """Роль в AI: адміністраторам Minerva (суперкористувач, ролі superadmin / admin) — завжди Admin, бо вони й так
    бачать і змінюють усе; іншим — «Роль в AI асистенті» з профілю (за замовчуванням Viewer)."""
    if not profile:
        return 'viewer'
    from core.utils import is_minerva_admin
    if is_minerva_admin(profile.user):
        return 'admin'
    role = profile.ai_assistant_role or 'viewer'
    return role if role in ROLE_TOOLS else 'viewer'


def role_label(role) -> str:
    return dict(UserProfile.AI_ROLE_CHOICES).get(role, role)


def capabilities(profile) -> list:
    """[{tool, label, on, why}] — що Minerva AI вміє для цієї людини, а що ні й чому (за роллю, «🔒 Дозволи»)."""
    from strategy.models import AISettings
    settings = AISettings.get()
    role = effective_role(profile)
    allowed = set(ROLE_TOOLS[role])
    out = []
    for tool in ROLE_TOOLS['admin']:
        why = ''
        if tool not in allowed:
            need = next(r for r in ('staff', 'manager', 'admin') if tool in ROLE_TOOLS[r])
            why = (f'ваша роль в AI — «{role_label(role)}», потрібна «{role_label(need)}»: її вибирає адміністратор '
                   f'у профілі → «Роль в AI асистенті»')
        elif tool in GLOBAL_SWITCHES and not getattr(settings, GLOBAL_SWITCHES[tool][0]):
            why = f'вимкнено для всіх: «⚙️ Налаштування AI» → «🔒 Дозволи» → «{GLOBAL_SWITCHES[tool][1]}»'
        out.append({'tool': tool, 'label': TOOL_LABELS.get(tool, tool), 'on': not why, 'why': why})
    return out


def get_allowed_tools(profile) -> list:
    """
    Повертає список allowed tool schemas для Anthropic API.
    Пріоритет: глобальні заборони AISettings > роль юзера
    """
    from ai_assistant.tools import ALL_TOOLS
    allowed_names = {c['tool'] for c in capabilities(profile) if c['on']}
    return [t for t in ALL_TOOLS if t['name'] in allowed_names]


def build_user_context(profile) -> str:
    """Контекстний рядок про юзера для system prompt: роль і чого цій людині недоступно (щоб пояснити, а не
    відмовити загадкою)."""
    if not profile:
        return 'Юзер: гість. Роль: viewer. Доступ обмежений.'

    lines = [
        f"Юзер: {profile.user.get_full_name() or profile.user.username}",
        f"Роль в AI: {role_label(effective_role(profile))}",
    ]
    off = [c for c in capabilities(profile) if not c['on']]
    if off:
        lines.append('НЕДОСТУПНО ЦЬОМУ ЮЗЕРУ (на прохання — поясни коротко, що саме й чому, як це ввімкнути, і де '
                     'зробити інакше; лист можна написати самому в «📥 Пошта → ✍️ Написати» з AI-відповіддю):')
        lines += [f"- {c['label']} — {c['why']}" for c in off]
    if profile.notes:
        lines.append(f"Контекст: {profile.notes[:200]}")
    return '\n'.join(lines)
