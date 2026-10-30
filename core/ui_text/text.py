"""Як фраза інтерфейсу порівнюється зі словником — однаково при зборі (extract.py) і в браузері
(static/admin/js/mv_translate.js, функція `edges`): по краях знімаються пробіли, емодзі й значки, тире, «·», «:»
тощо (їх переклад не міняє), пробіли всередині зводяться до одного."""
from __future__ import annotations

import unicodedata

EDGE_EXTRA = set("  \t\r\n·•|:;,*›‹‍️⁦⁩")


def _edge(ch: str) -> bool:
    if ch in EDGE_EXTRA:
        return True
    cat = unicodedata.category(ch)
    return cat[0] == "S" or cat in ("Pd", "Zs", "Cf")


def edges(text: str) -> tuple[str, str, str]:
    """(те, що на початку, ядро фрази, те, що в кінці)."""
    text = text or ""
    i, j = 0, len(text)
    while i < j and _edge(text[i]):
        i += 1
    while j > i and _edge(text[j - 1]):
        j -= 1
    return text[:i], " ".join(text[i:j].split()), text[j:]
