"""
Тексти відповідей Telegram-бота без залежності від python-telegram-bot (тестуються без бібліотеки).
"""


def myid_text(user_id, chat_id=None, chat_type: str = 'private') -> str:
    """
    Відповідь на /myid: Telegram ID того, хто написав (і ID групи, якщо це група),
    та чи прив'язаний цей ID до акаунту Minerva. Нічого більше: ні імені, ні ролі, ні даних акаунту.
    """
    from ai_assistant.permissions import get_profile_for_telegram

    linked = get_profile_for_telegram(user_id) is not None
    lines = [f"🆔 Ваш Telegram ID: `{user_id}`"]
    if chat_type in ('group', 'supergroup', 'channel') and chat_id is not None and chat_id != user_id:
        lines.append(f"💬 ID цього чату: `{chat_id}`")
    lines.append('')
    if linked:
        lines.append("✅ Цей Telegram уже прив'язаний до акаунту Minerva.")
    else:
        lines.append("⚠️ Цей Telegram ще не прив'язаний до акаунту Minerva.")
        lines.append("Впишіть ID у «Мої налаштування» → «Telegram ID» або передайте його адміністратору.")
    return '\n'.join(lines)
