"""
suppliers/app_api.py — JSON API для 📱 додатка «Minerva Візитки» (/suppliers/app/api/…, mobile/cards_app).

Вхід — ключ телефону «Authorization: Bearer mvc_…» (suppliers/services/card_app.py), не сесія й не CSRF; права —
ролі Minerva людини, під якою підключено телефон: візитки — inventory.add|change|view_supplier (як сторінки
«📷 Візитка / флаєр» і «🪪 Візитки й флаєри»), категорії — suppliers.add|change_suppliercategory.

  POST pair/                     {code, device, version} → {token, user} — QR зі сторінки «📱 Додаток на телефон»;
  GET  me/                       — хто підключений, права;
  GET  meta/                     — категорії, пресети, види, позначки, моделі AI, недавні «де взяли»;
  POST categories/ | categories/<id>/ — нова категорія / правка;
  GET|POST presets/              — пресети (action save | delete | default);
  GET  cards/                    — список з фільтрами (q, cat, ai, event, mark, mine, show, page) або ids=…;
  POST cards/                    — нова візитка (multipart: image, image_back, uuid, kind, event, note, marks,
                                   categories, preset, read_ai, model, qr_front, qr_back); той самий uuid — та сама;
  GET|POST cards/<id>/           — візитка / правка (note, event, title, kind, marks, categories);
  POST cards/<id>/read/          — 🧠 прочитати AI; cards/<id>/to-supplier/ — 🏭 у постачальники;
  POST cards/<id>/to-customer/   — 👤 у CRM (той самий email / фірма — до нього, немає — новий клієнт);
  POST cards/<id>/delete/        — 🗑; GET cards/<id>/image/?side=front|back|p<id>&size=thumb|full — фото;
  POST cards/<id>/side/          — 🔄 перезняли лицьовий / зворотний бік (multipart: side, image, qr) або
                                   прибрати зворотний (side=back, delete=1) — suppliers/services/card_photos.py;
  POST cards/<id>/photos/        — 📎 ще фото групи (multipart: image, kind, uuid, note, qr; той самий uuid — те саме);
  POST cards/<id>/photos/<pid>/delete/ — 🗑 прибрати ще фото. AI — окремо (cards/<id>/read/), раз після всіх змін.
"""
import functools
import json
import logging
import mimetypes
import os

from django.http import FileResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)
PHOTO_MAX = 15 * 1024 * 1024


class _Data(dict):
    """JSON-тіло або форма (multipart) з однаковим доступом: get, getlist (список / «a,b»)."""

    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x) for x in v]
        return [x for x in str(v).split(",") if x != ""]


def _data(request) -> _Data:
    if (request.content_type or "").startswith("application/json"):
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            body = {}
        return _Data(body if isinstance(body, dict) else {})
    d = _Data({k: request.POST.get(k) for k in request.POST})
    for k in request.POST:
        values = request.POST.getlist(k)
        if len(values) > 1:
            d[k] = values
    return d


def _ip(request):
    from core.utils import client_ip
    try:
        return client_ip(request)
    except Exception:  # noqa: BLE001
        return request.META.get("REMOTE_ADDR")


def _err(text, status=400):
    return JsonResponse({"error": str(text)}, status=status)


def api(methods=("GET",)):
    """Ключ телефону → request.device / request.app_user; інший метод — 405; збій — JSON 500 (у журналі)."""
    def wrap(view):
        @csrf_exempt
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            from suppliers.services import card_app
            if request.method not in methods:
                resp = _err("Метод не дозволено", 405)
                resp["Allow"] = ", ".join(methods)
                return resp
            device = card_app.device_for(request.META.get("HTTP_AUTHORIZATION"), _ip(request))
            if device is None:
                return _err("Телефон не підключено або відключено в Minerva — підключіть знову QR-кодом.", 401)
            request.device, request.app_user = device, device.user
            try:
                return view(request, *args, **kwargs)
            except PermissionError as e:
                return _err(str(e) or "Немає права", 403)
            except Exception:  # noqa: BLE001
                logger.exception("cards app api failed: %s", request.path)
                return _err("Помилка на сервері Minerva — спробуйте ще раз.", 500)
        return inner
    return wrap


@csrf_exempt
def pair(request):
    from suppliers.services import card_app
    if request.method != "POST":
        return _err("Метод не дозволено", 405)
    d = _data(request)
    try:
        device, token = card_app.pair(d.get("code"), d.get("device"), d.get("version"), _ip(request))
    except card_app.PairError as e:
        return _err(str(e), e.status)
    u = device.user
    return JsonResponse({"token": token, "device": device.pk,
                         "user": {"name": u.get_full_name() or u.username, "email": u.email}})


@api()
def me(request):
    from suppliers.services import card_app
    u, dev = request.app_user, request.device
    return JsonResponse({"user": {"name": u.get_full_name() or u.username, "email": u.email},
                         "device": {"id": dev.pk, "name": dev.name},
                         "perms": card_app.meta(u)["perms"]})


@api()
def meta(request):
    from suppliers.services import card_app
    if not card_app.can(request.app_user, "view"):
        raise PermissionError("Немає доступу до постачальників у Minerva.")
    return JsonResponse(card_app.meta(request.app_user))


@api(("POST",))
def categories(request, pk=None):
    from suppliers.models import SupplierCategory
    from suppliers.services import card_app
    cat = None
    if pk is not None:
        cat = SupplierCategory.objects.filter(pk=pk).first()
        if cat is None:
            return _err("Категорії немає", 404)
    try:
        c = card_app.save_category(request.app_user, _data(request), cat)
    except ValueError as e:
        return _err(e)
    return JsonResponse({"category": card_app.category_json(c),
                         "categories": [card_app.category_json(x)
                                        for x in SupplierCategory.objects.filter(is_active=True)]})


@api(("GET", "POST"))
def presets(request):
    from suppliers.services import card_app
    u = request.app_user
    if not card_app.can_upload(u):
        raise PermissionError("Немає права додавати візитки.")
    if request.method == "GET":
        return JsonResponse({"presets": card_app.presets_for(u), "default": card_app.default_preset(u)})
    d = _data(request)
    data = dict(d)
    data.update(marks=d.getlist("marks"), categories=d.getlist("categories"))
    try:
        return JsonResponse(card_app.save_preset(u, data))
    except ValueError as e:
        return _err(e)


def _card(request, pk):
    from suppliers.services import card_app
    if not card_app.can(request.app_user, "view"):
        raise PermissionError("Немає доступу до постачальників у Minerva.")
    return card_app.cards_qs().filter(pk=pk).first()


@api(("GET", "POST"))
def cards(request):
    from suppliers.models import SupplierCategory, SupplierMedia
    from suppliers.services import card_app, cards as card_svc
    u = request.app_user
    if request.method == "GET":
        if not card_app.can(u, "view"):
            raise PermissionError("Немає доступу до постачальників у Minerva.")
        items, more = card_app.search(u, request.GET)
        return JsonResponse({"items": items, "more": more})
    if not card_app.can_upload(u):
        raise PermissionError("Немає права додавати візитки.")
    d = _data(request)
    uuid = str(d.get("uuid") or "")[:40]
    if uuid:                                   # повтор після обриву зв'язку — та сама візитка
        old = card_app.cards_qs().filter(client_uuid=uuid, created_by=u).first()
        if old is not None:
            return JsonResponse({"card": card_app.card_json(old, u), "created": False})
    image, back = request.FILES.get("image"), request.FILES.get("image_back")
    if image is None:
        return _err("Немає фото.")
    for f in (image, back):
        if f is not None and (f.size > PHOTO_MAX or not card_app.is_image(f)):
            return _err("Фото понад 15 МБ або не картинка.")
    cats = list(SupplierCategory.objects.filter(pk__in=card_app._ids(d.getlist("categories"))))
    m = card_svc.create(u, image, back, kind=d.get("kind"), title=d.get("title") or "", event=d.get("event") or "",
                        note=d.get("note") or "", marks=d.getlist("marks"), categories=cats,
                        preset_id=d.get("preset"), qr_front=d.get("qr_front") or "", qr_back=d.get("qr_back") or "",
                        client_uuid=uuid, role=d.get("role") or "")
    job = ""
    if card_app._bool(d.get("read_ai")):
        job = card_app.start_read(m, u, d.get("model"))
    m = card_app.cards_qs().get(pk=m.pk)
    return JsonResponse({"card": card_app.card_json(m, u), "created": True, "job": job,
                         "state": SupplierMedia.objects.filter(pk=m.pk).values_list("state", flat=True).first()},
                        status=201)


@api(("GET", "POST"))
def card(request, pk):
    from suppliers.models import SupplierCategory, SupplierMedia
    from suppliers.services import card_app
    u = request.app_user
    m = _card(request, pk)
    if m is None:
        return _err("Візитки немає — можливо, її видалили в Minerva.", 404)
    if request.method == "POST":
        if not card_app.can_edit(u, m):
            raise PermissionError("Це фото іншої людини, а права змінювати постачальників у вас немає.")
        d = _data(request)
        fields = []
        for k, limit in (("note", 4000), ("event", 200), ("title", 200)):
            if k in d:
                setattr(m, k, str(d.get(k) or "")[:limit])
                fields.append(k)
        if d.get("kind") in dict(SupplierMedia.KINDS):
            m.kind = d["kind"]
            fields.append("kind")
        if "marks" in d:
            m.marks = [x for x in d.getlist("marks") if x in dict(SupplierMedia.MARKS)]
            fields.append("marks")
        if "role" in d and (d.get("role") or "") in dict(SupplierMedia.ROLES):
            m.role, m.role_ai = d.get("role") or "", False          # людина вибрала — AI вже не міняє
            fields += ["role", "role_ai"]
        if fields:
            m.save(update_fields=fields)
        if "categories" in d:
            m.categories.set(SupplierCategory.objects.filter(pk__in=card_app._ids(d.getlist("categories"))))
        m = _card(request, pk)
    return JsonResponse({"card": card_app.card_json(m, u)})


@api(("POST",))
def card_read(request, pk):
    from suppliers.services import card_app
    u = request.app_user
    m = _card(request, pk)
    if m is None:
        return _err("Візитки немає", 404)
    if not card_app.can_upload(u):
        raise PermissionError("Немає права читати візитки AI.")
    job = card_app.start_read(m, u, _data(request).get("model"))
    return JsonResponse({"job": job, "card": card_app.card_json(m, u)})


@api(("POST",))
def card_to_supplier(request, pk):
    from suppliers.services import card_app, cards as card_svc
    u = request.app_user
    m = _card(request, pk)
    if m is None:
        return _err("Візитки немає", 404)
    if not card_app.can_edit(u, m):
        raise PermissionError("Немає права вносити цю візитку.")
    out = card_svc.to_suppliers([m], u, can_add=card_app.can(u, "add"), can_change=card_app.can(u, "change"))
    done = out["created"] + out["attached"]
    m = _card(request, pk)
    return JsonResponse({"card": card_app.card_json(m, u),
                         "created": bool(out["created"]), "attached": bool(out["attached"]),
                         "supplier": {"id": done[0][1].pk, "name": done[0][1].name} if done else None,
                         "skipped": out["skipped"], "filled": out["filled"]})


@api(("POST",))
def card_to_customer(request, pk):
    """👤 Візитка → клієнт CRM (як «👤 У CRM» для вибраних): той самий email / фірма — до нього, немає — новий."""
    from suppliers.services import card_app, cards as card_svc
    u = request.app_user
    m = _card(request, pk)
    if m is None:
        return _err("Візитки немає", 404)
    if not card_app.can_edit(u, m):
        raise PermissionError("Немає права вносити цю візитку.")
    if not (u.has_perm("crm.add_customer") or u.has_perm("crm.change_customer")):
        raise PermissionError("Немає права вносити клієнтів у CRM.")
    out = card_svc.to_customers([m], u, can_add=u.has_perm("crm.add_customer"),
                                can_change=u.has_perm("crm.change_customer"))
    done = out["created"] + out["attached"]
    m = _card(request, pk)
    return JsonResponse({"card": card_app.card_json(m, u), "created": bool(out["created"]),
                         "attached": bool(out["attached"]),
                         "customer": {"id": done[0][1].pk, "name": done[0][1].company or done[0][1].name} if done else None,
                         "skipped": out["skipped"]})


@api(("POST",))
def card_delete(request, pk):
    from suppliers.services import card_app, cards as card_svc
    u = request.app_user
    m = _card(request, pk)
    if m is None:
        return JsonResponse({"ok": True, "gone": True})
    if not card_app.can_edit(u, m):
        raise PermissionError("Це фото іншої людини, а права змінювати постачальників у вас немає.")
    card_svc.delete(m)
    return JsonResponse({"ok": True})


def _photo_upload(request):
    """Фото з телефона: є, ≤ 15 МБ і справді картинка → (файл, ""); інакше — (None, текст помилки)."""
    from suppliers.services import card_app
    f = request.FILES.get("image")
    if f is None:
        return None, "Немає фото."
    if f.size > PHOTO_MAX or not card_app.is_image(f):
        return None, "Фото понад 15 МБ або не картинка."
    return f, ""


def _editable(request, pk):
    from suppliers.services import card_app
    m = _card(request, pk)
    if m is not None and not card_app.can_edit(request.app_user, m):
        raise PermissionError("Це фото іншої людини, а права змінювати постачальників у вас немає.")
    return m


@api(("POST",))
def card_side(request, pk):
    """🔄 Лицьовий / зворотний бік — нове фото (перезняли, дознімали зворотний) або 🗑 прибрати зворотний."""
    from suppliers.services import card_app, card_photos
    m = _editable(request, pk)
    if m is None:
        return _err("Візитки немає — можливо, її видалили в Minerva.", 404)
    d = _data(request)
    side = "back" if d.get("side") == "back" else "front"
    if card_app._bool(d.get("delete")):
        if side != "back":
            return _err("Лицьовий бік не прибрати — зніміть його знову.")
        if m.image_back:
            card_photos.drop_back(m)
    else:
        f, error = _photo_upload(request)
        if f is None:
            return _err(error)
        card_photos.set_side(m, side, f, d.get("qr") or "")
    return JsonResponse({"card": card_app.card_json(_card(request, pk), request.app_user)})


@api(("POST",))
def card_photos_add(request, pk):
    """📎 Ще фото до візитки: друга візитка (інша людина), флаєр, каталог, продукт, стенд."""
    from suppliers.services import card_app, card_photos
    m = _editable(request, pk)
    if m is None:
        return _err("Візитки немає — можливо, її видалили в Minerva.", 404)
    d = _data(request)
    uuid = str(d.get("uuid") or "")[:40]
    p = m.photos.filter(client_uuid=uuid).first() if uuid else None
    if p is None:
        f, error = _photo_upload(request)
        if f is None:
            return _err(error)
        try:
            p = card_photos.add(m, f, d.get("kind"), request.app_user, uuid=uuid, note=d.get("note") or "",
                                qr=d.get("qr") or "")
        except ValueError as e:
            return _err(e)
    return JsonResponse({"photo": {"id": p.pk, "kind": p.kind, "uuid": p.client_uuid},
                         "card": card_app.card_json(_card(request, pk), request.app_user)})


@api(("POST",))
def card_photo_delete(request, pk, photo_pk):
    """🗑 Прибрати ще фото (уже немає — теж добре: телефон повторює після обриву зв'язку)."""
    from suppliers.services import card_app, card_photos
    m = _editable(request, pk)
    if m is None:
        return JsonResponse({"ok": True, "gone": True})
    p = m.photos.filter(pk=photo_pk).first()
    if p is not None:
        card_photos.remove(p)
    return JsonResponse({"ok": True, "card": card_app.card_json(_card(request, pk), request.app_user)})


@api()
def card_image(request, pk):
    from suppliers.services import card_app
    m = _card(request, pk)
    side = str(request.GET.get("side") or "front")
    if side not in ("front", "back") and not (side.startswith("p") and side[1:].isdigit()):
        side = "front"
    field = None if m is None else card_app.side_field(m, side)
    if not field:
        return _err("Фото немає", 404)
    if request.GET.get("size") != "full":
        path = card_app.thumb(m, side)
        if path:
            resp = FileResponse(open(path, "rb"), content_type="image/jpeg")
            resp["Cache-Control"] = "private, max-age=86400"
            return resp
    ctype = mimetypes.guess_type(field.name)[0] or "application/octet-stream"
    if not ctype.startswith("image/") or ctype == "image/svg+xml":
        ctype = "application/octet-stream"
    resp = FileResponse(field.open("rb"), content_type=ctype)
    resp["Cache-Control"] = "private, max-age=86400"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Disposition"] = f'inline; filename="{os.path.basename(field.name)}"'
    return resp
