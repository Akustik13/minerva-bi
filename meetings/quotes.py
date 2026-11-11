"""Цитата ключового моменту в розшифровці — ті самі правила, що на RAG-ПК (meetings/rag_pc/bench/moments.py:
normalize + place). Спільні випадки для обох боків — meetings/tests_quote_cases.py (CASES): Minerva перевіряє їх
у tests_quotes.py, RAG-ПК — своїми тестами, тож пункт, який прийняв RAG-ПК, Minerva не відкине.

Лише стандартна бібліотека (без Django).
"""
import hashlib
import re

_QUOTES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'", "“": '"', "”": '"', "„": '"',
                         "«": '"', "»": '"', "–": "-", "—": "-", " ": " "})
MIN_CHARS = 8           # коротша цитата нічого не доводить (як на RAG-ПК)


def normalize(text) -> str:
    """Регістр, лапки, тире, пробіли — однаково; розділові знаки по краях прибрано."""
    text = (text or "").translate(_QUOTES).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,;:!?-\"'…")


def place(quote, line_no, by_no, ordered=None):
    """Номер рядка, де цитата справді є, або None. Порядок: сам рядок → сусіди ±1 → рядок разом із сусідами
    (речення, розірване на два сегменти; прив'язка — до line_no) → будь-який рядок.

    by_no — {n: текст}; ordered — номери рядків у порядку часу (за замовчуванням — за зростанням)."""
    q = normalize(quote)
    if len(q) < MIN_CHARS:
        return None
    if line_no in by_no and q in normalize(by_no[line_no]):
        return line_no
    near = [n for n in (line_no - 1, line_no + 1) if n in by_no]
    for n in near:
        if q in normalize(by_no[n]):
            return n
    if line_no in by_no and near:
        joined = normalize(" ".join(by_no[n] for n in sorted([line_no, *near])))
        if q in joined:
            return line_no
    for n in (ordered if ordered is not None else sorted(by_no)):
        if q in normalize(by_no.get(n, "")):
            return n
    return None


def find(quote, line_no, segments):
    """place() для розшифровки Minerva: segments — [{n, text, …}]; → n або None."""
    by_no = {s["n"]: s.get("text") or "" for s in segments if isinstance(s, dict) and s.get("n") is not None}
    try:
        line = int(line_no)
    except (TypeError, ValueError):
        line = -1
    return place(quote, line, by_no, [s["n"] for s in segments if isinstance(s, dict) and s.get("n") is not None])


def item_key(kind, quote) -> str:
    """Ключ пункту наради: той самий вид і та сама цитата → той самий ключ (✕ і ✓ не повертаються при повторі)."""
    return hashlib.sha1(f"{kind}|{normalize(quote)[:300]}".encode("utf-8")).hexdigest()[:40]


def mark_key(t) -> str:
    """Ключ 📌 позначки під час запису — за секундою (з точністю до 0,1 с)."""
    try:
        t = round(float(t), 1)
    except (TypeError, ValueError):
        t = 0.0
    return hashlib.sha1(f"mark|{t}".encode("utf-8")).hexdigest()[:40]
