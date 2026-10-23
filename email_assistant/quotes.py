"""Текст листа без процитованої історії — спільне для пошти («📋 Підготувати») і Angebote (offers.detection)."""
import re

# Початок процитованої історії; «Am 01.10.2026 um 10:00 schrieb Max Muster <max@x.de>:» — ім'я після «schrieb»
QUOTE_START_RE = re.compile(
    r"^\s*(?:>|-{2,}\s*(?:original|ursprüngliche|forwarded|weitergeleitete)"
    r"|(?:on|am)\s.{5,80}(?:wrote|schrieb)(?:\s[^\n]{0,120}?)?\s*:"
    r"|von:\s|from:\s|gesendet:\s|sent:\s|від:\s|от:\s)", re.IGNORECASE)


def new_text(body) -> str:
    """Текст листа без процитованої історії (відповідь клієнта зверху, далі — наш попередній лист)."""
    out = []
    for line in (body or "").splitlines():
        if QUOTE_START_RE.match(line):
            break
        out.append(line)
    return "\n".join(out)[:20000]
