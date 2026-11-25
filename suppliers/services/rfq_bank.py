"""📂 Банк файлів запиту постачальникам: усі вкладення, які надіслали постачальники (SupplierReplyFile), і файли нашого
запиту — для вікна «📂 Файли» на сторінці запиту (request_work.html, _request_talk.html): по постачальниках у порядку
таблиці, з типом файлу (core.file_preview.kind) для фільтра, перегляд — сторінка файлу з ?embed=1 у вікні.

  bank(req, order) → {groups: [{rid, n, name, files: [{id, name, icon, kind, size, at, embed, page, raw, dl, pdf}]}], count}
  order — отримувачі в порядку таблиці (№ рядка = позиція).
"""
from django.urls import reverse
from django.utils import timezone

KIND_GROUP = {"pdf": "pdf", "sheet": "sheet", "docx": "doc", "office": "doc", "image": "image", "archive": "archive",
              "text": "other", "none": "other"}


def _at(dt) -> str:
    return timezone.localtime(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def _item(req, f, src, when) -> dict:
    from core import file_preview as fp
    url = reverse("admin:suppliers_supplierrequest_file", args=[req.pk, src, f.pk])
    kind = fp.kind(f.name)
    return {"id": f"{src}:{f.pk}", "name": f.name, "icon": fp.ICONS.get(kind, "📎"), "kind": KIND_GROUP.get(kind, "other"),
            "size": fp.size_text(f.size), "at": _at(when), "embed": url + "?embed=1", "page": url,
            "raw": url + "?raw=1", "dl": url + "?download=1", "pdf": kind in ("pdf", "image")}


def bank(req, order) -> dict:
    from suppliers.models import SupplierReplyFile
    by = {}
    for f in SupplierReplyFile.objects.filter(recipient__request=req).order_by("-received_at", "-pk"):
        if f.file:
            by.setdefault(f.recipient_id, []).append(f)
    groups = []
    for pos, r in enumerate(order, 1):
        files = by.get(r.pk) or []
        if files:
            groups.append({"rid": r.pk, "n": pos, "name": r.supplier.name,
                           "files": [_item(req, f, "reply", f.received_at or f.created_at) for f in files]})
    ours = [f for f in req.files.all() if f.file]
    if ours:
        groups.append({"rid": 0, "n": "", "name": "📤 Файли нашого запиту",
                       "files": [_item(req, f, "req", f.created_at) for f in ours]})
    return {"groups": groups, "count": sum(len(g["files"]) for g in groups)}
