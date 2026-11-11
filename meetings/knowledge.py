"""🎙 Розшифровка наради → приватні документи бази знань («📚 Додати в мою базу знань» на сторінці наради).

Частини ≤ rag_assistant.knowledge.TEXT_MAX (100 000) знаків, поділ — по рядках «[ГГ:ХХ:СС] Я: …» (задовгий рядок
ріжеться). KnowledgeItem: kind MEETING (для RAG — «link»: текст дала Minerva), auto (у 📎 картки без 👥 / ✕),
visibility private — бачить і знаходить лише той, хто записав; проєкт і клієнт наради; назва нейтральна
«🎙 Нарада P-… · ДД.ММ.РРРР · n/N» (без назви наради); created_at — день наради. Хмарні AI-помічники Minerva цих
документів не отримують (rag_assistant.knowledge_items.ai_blocked).

unpublish — прибрати всі частини (tombstone у стрічці для RAG) і копії тексту у збережених результатах пошуку RAG,
«🧭 / ❓» карток і брифах відповіді (RetrievalJob.hits, EntityHelp.sources, ReplyBrief.sources) власника наради.
"""
import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

PREFIX = "meeting:{pk}:"
HEADER_RESERVE = 400                    # місце під рядок-заголовок частини


def prefix(meeting_or_pk) -> str:
    return PREFIX.format(pk=getattr(meeting_or_pk, "pk", meeting_or_pk))


def clock(seconds) -> str:
    s = max(int(seconds or 0), 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def lines(meeting, segments) -> list:
    from .services import speaker_labels
    labels = speaker_labels(meeting)
    out = []
    for seg in segments or []:
        text = " ".join(str(seg.get("text") or "").split())
        if text:
            out.append(f"[{clock(seg.get('start'))}] {labels.get(seg.get('ch'), labels['M'])}: {text}")
    return out


def header(meeting, n, total) -> str:
    from .models import Meeting
    from .services import user_name
    day = f"{meeting.day:%d.%m.%Y}"
    head = f"🎙 Нарада {day} · частина {n} з {total}."
    mode = meeting.capture_mode or ""
    if mode == Meeting.MIC:
        return head + " Записано мікрофоном у кімнаті — мовців не розділено («У кімнаті (мікрофон)»)."
    if mode == Meeting.MEETING_ONLY:
        return head + " Записано лише звук наради («Нарада»)."
    who = user_name(meeting.owner) if meeting.owner_id else ""
    return head + f" «Я» — {who or 'той, хто записав'}, «Співрозмовники» — інші учасники."


def split(text_lines, budget) -> list:
    """Частини тексту ≤ budget знаків, межі — між рядками; рядок, довший за budget, ріжеться."""
    parts, cur, size = [], [], 0
    for line in text_lines:
        while len(line) > budget:
            if cur:
                parts.append("\n".join(cur))
                cur, size = [], 0
            parts.append(line[:budget])
            line = line[budget:]
        if not line:
            continue
        add = len(line) + (1 if cur else 0)
        if cur and size + add > budget:
            parts.append("\n".join(cur))
            cur, size = [], 0
            add = len(line)
        cur.append(line)
        size += add
    if cur:
        parts.append("\n".join(cur))
    return parts


def title_for(meeting, n, total) -> str:
    code = f" {meeting.project.code}" if meeting.project_id else ""
    return f"🎙 Нарада{code} · {meeting.day:%d.%m.%Y} · {n}/{total}"[:300]


def build(meeting, segments) -> list:
    """[(назва, текст)] частин."""
    from rag_assistant.knowledge import TEXT_MAX
    body = split(lines(meeting, segments), TEXT_MAX - HEADER_RESERVE)
    total = max(len(body), 1)
    if not body:
        return []
    return [(title_for(meeting, n, total), f"{header(meeting, n, total)}\n{text}"[:TEXT_MAX])
            for n, text in enumerate(body, 1)]


def publish(meeting) -> list:
    """Затвердити розшифровку як приватні документи бази знань власника (повторно — оновити ті самі частини;
    зайві старі прибираються). Хто може — перевіряє сторінка (лише власник). → pk документів."""
    from rag_assistant import knowledge_items
    from rag_assistant.models import KnowledgeItem
    from .models import Meeting, MeetingTranscript
    from .services import MeetingError
    m = Meeting.objects.select_related("owner", "project", "customer", "project__customer").get(pk=meeting.pk)
    if not m.has_transcript:
        raise MeetingError("Розшифровки ще немає або її вже видалено")
    if not m.owner_id or not m.owner.is_active:
        raise MeetingError("Немає власника наради")
    tr = MeetingTranscript.objects.filter(meeting=m).first()
    parts = build(m, tr.segments if tr else [])
    if not parts:
        raise MeetingError("У розшифровці немає тексту")
    pre = prefix(m)
    customer = m.customer or (m.project.customer if m.project_id else None)
    old = {i.source_key: i for i in KnowledgeItem.objects.filter(source_key__startswith=pre)}
    pks = []
    with transaction.atomic():
        with knowledge_items.quiet():
            for n, (title, text) in enumerate(parts, 1):
                key = f"{pre}{n}"
                item = old.pop(key, None) or KnowledgeItem(source_key=key)
                item.kind, item.auto, item.visibility = KnowledgeItem.MEETING, True, KnowledgeItem.PRIVATE
                item.owner, item.project, item.customer = m.owner, m.project, customer
                item.order = item.product = item.offer = None
                item.title, item.text, item.note, item.url = title, text, "", ""
                item.save()
                pks.append(item.pk)
            gone = [i.pk for i in old.values()]
            KnowledgeItem.objects.filter(pk__in=gone).delete()
            KnowledgeItem.objects.filter(pk__in=pks).update(created_at=m.started_at or m.created_at)
    knowledge_items.touch(pks + gone)
    now = timezone.now()
    Meeting.objects.filter(pk=m.pk).update(approved_at=now, updated_at=now)
    meeting.approved_at = now
    return pks


def unpublish(meeting_or_pk) -> int:
    """Прибрати всі частини наради з бази знань (tombstone → RAG прибере вектори) і копії їхнього тексту.
    Префікс із двокрапкою в кінці: нарада 12 не зачіпає наради 120."""
    try:
        from rag_assistant import knowledge_items
        from rag_assistant.models import KnowledgeItem
    except ImportError:
        return 0
    pk = getattr(meeting_or_pk, "pk", meeting_or_pk)
    qs = KnowledgeItem.objects.filter(source_key__startswith=prefix(pk))
    pks = list(qs.values_list("pk", flat=True))
    if not pks:
        return 0
    owner_id = getattr(meeting_or_pk, "owner_id", None)
    if owner_id is None and not hasattr(meeting_or_pk, "owner_id"):
        from .models import Meeting
        owner_id = Meeting.objects.filter(pk=pk).values_list("owner_id", flat=True).first()
    with knowledge_items.quiet():
        qs.delete()
    knowledge_items.touch(pks)
    scrub_copies(owner_id, pks)
    return len(pks)


def scrub_copies(owner_id, item_pks) -> int:
    """Копії тексту частин у збережених результатах власника: фрагменти пошуку RAG — геть, джерела «🧭 / ❓» і брифів —
    без тексту (номери [#N] не зсуваються). Користувача видалено — його записи вже видалено каскадом."""
    if not owner_id or not item_pks:
        return 0
    docs = {f"document:{pk}" for pk in item_pks}
    urls = {f"/rag/knowledge/{pk}/" for pk in item_pks}
    changed = 0

    def hit_of(h):
        return isinstance(h, dict) and (h.get("document_id") in docs or h.get("url") in urls)

    try:
        from rag_assistant.models import EntityHelp, RetrievalJob
        for job in RetrievalJob.objects.filter(user_id=owner_id).only("id", "hits").iterator(chunk_size=200):
            if any(hit_of(h) for h in job.hits or []):
                RetrievalJob.objects.filter(pk=job.pk).update(hits=[h for h in job.hits if not hit_of(h)])
                changed += 1
        for row in EntityHelp.objects.filter(user_id=owner_id).only("id", "sources").iterator(chunk_size=200):
            if any(hit_of(src) for src in row.sources or []):
                EntityHelp.objects.filter(pk=row.pk).update(sources=_blank(row.sources, hit_of))
                changed += 1
    except Exception as e:  # noqa: BLE001 — прибирання копій не ламає видалення
        logger.warning("meeting copies scrub (rag): %s", e)
    try:
        from email_assistant.models import ReplyBrief
        for row in ReplyBrief.objects.filter(user_id=owner_id).only("id", "sources").iterator(chunk_size=200):
            if any(hit_of(src) for src in row.sources or []):
                ReplyBrief.objects.filter(pk=row.pk).update(sources=_blank(row.sources, hit_of))
                changed += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("meeting copies scrub (briefs): %s", e)
    return changed


def _blank(sources, hit_of) -> list:
    return [dict(src, text="", label="🎙 (розшифровку наради видалено)") if hit_of(src) else src
            for src in sources or []]


def parts(meeting):
    from rag_assistant.models import KnowledgeItem
    items = list(KnowledgeItem.objects.filter(source_key__startswith=prefix(meeting)))

    def n_of(item):
        tail = item.source_key.rsplit(":", 1)[-1]
        return int(tail) if tail.isdigit() else 0
    return sorted(items, key=n_of)


def parts_state(meeting, user) -> dict:
    """Для «📚 База знань» на сторінці наради (лише власнику): {enabled, note, parts: [{n, pk, title, rag}]}."""
    out = {"enabled": False, "note": "", "parts": []}
    if user is None or meeting.owner_id != getattr(user, "pk", None):
        return out
    try:
        from rag_assistant import knowledge_items, retrieval
        from rag_assistant.models import KnowledgeDoc, KnowledgeSettings
        from rag_assistant.views_entity import rag_text
    except ImportError:
        return out
    out["enabled"] = bool(KnowledgeSettings.get().enabled)
    if not out["enabled"]:
        out["note"] = ("Індексацію бази знань вимкнено — документи знайдуться пошуком, коли адміністратор її "
                       "ввімкне («🧠 База знань»).")
    supported = retrieval.documents_supported()
    items = parts(meeting)
    docs = {d.obj_id: d for d in KnowledgeDoc.objects.filter(doc_type=knowledge_items.DOC_TYPE,
                                                             obj_id__in=[i.pk for i in items])}
    for n, item in enumerate(items, 1):
        out["parts"].append({"n": n, "pk": item.pk, "title": item.title,
                             "rag": rag_text(item, docs.get(item.pk), supported)})
    return out
