"""
suppliers/services/card_photos.py — 📎 група фото однієї візитки: лицьовий і зворотний бік (SupplierMedia.image /
image_back) і ще фото (SupplierMediaPhoto): друга візитка тієї самої фірми (інша людина), флаєр, каталог, продукт,
стенд. Додаток «Minerva Візитки» (suppliers/app_api.py) і сторінка фото (admin_supplier.card_review_view).

  add(m, upload, kind, user, uuid, note, qr) — ще фото (той самий uuid удруге — те саме фото; понад MAX — ValueError);
  remove(photo)                  — файл і запис;
  set_side(m, side, upload, qr)  — 🔄 перезняли лицьовий / зворотний бік (чи дознімали зворотний): старе фото й
                                   оригінал до правок — геть;
  drop_back(m)                   — 🗑 прибрати зворотний бік;
  rotate(photo, degrees)         — ⟳ повернути ще фото (сторінка фото; боки — «✏️ Повернути / обрізати»);
  files(m)                       — усі фото групи [(файл, суфікс назви)] — лист «✉️ Поділитись», «📎 Документи» CRM;
  for_ai(m)                      — фото для AI з підписами «Фото 1 — лицьовий бік візитки» …;
  clean_people(value)            — люди з інших візиток групи (відповідь AI → extracted["people"]);
  add_people(supplier, m, user)  — їх контактами постачальнику (уже є — дописати порожнє).
"""
import io
import os

MAX = 12                     # ще фото на одну візитку
AI_PHOTOS = 8                # фото групи в одному запиті AI (разом з боками)
AI_BYTES = 20 * 1024 * 1024  # і не більше стількох байт (запит AI — до 32 МБ, base64 + 33 %)
EXTRA_LABELS = {
    "card": "ще одна візитка цієї фірми (інша людина)", "flyer": "флаєр", "catalog": "каталог / прайс",
    "product": "продукт / зразок", "stand": "стенд на виставці", "other": "інше фото",
}


def kinds() -> list:
    from suppliers.models import SupplierMediaPhoto
    return SupplierMediaPhoto.KINDS


def clean_kind(value) -> str:
    from suppliers.models import SupplierMediaPhoto
    value = str(value or "")
    return value if value in dict(SupplierMediaPhoto.KINDS) else SupplierMediaPhoto.CARD


def add(m, upload, kind="card", user=None, uuid="", note="", qr=""):
    """📎 Ще фото до візитки → SupplierMediaPhoto. Повтор з тим самим uuid — те саме фото, без другого."""
    from suppliers.models import SupplierMediaPhoto
    uuid = str(uuid or "")[:40]
    if uuid:
        old = m.photos.filter(client_uuid=uuid).first()
        if old is not None:
            return old
    if m.photos.count() >= MAX:
        raise ValueError(f"До однієї візитки — не більше {MAX} фото.")
    last = m.photos.order_by("-order").values_list("order", flat=True).first() or 0
    p = SupplierMediaPhoto(media=m, kind=clean_kind(kind), note=str(note or "")[:300], qr_text=str(qr or "")[:4000],
                           order=last + 1, created_by=user, client_uuid=uuid)
    p.image.save(os.path.basename(getattr(upload, "name", "") or "") or "photo.jpg", upload, save=False)
    p.save()
    return p


def _drop_file(field_file, keep=()):
    if field_file and field_file.name not in keep:
        try:
            field_file.storage.delete(field_file.name)
        except Exception:  # noqa: BLE001 — файлу вже немає
            pass


def remove(photo):
    _drop_file(photo.image)
    photo.delete()


def set_side(m, side, upload, qr=""):
    """🔄 Лицьовий / зворотний бік — нове фото (перезняли або дознімали зворотний). Попереднє фото й оригінал до
    правок цього боку видаляються; QR нового фото дописується до QR візитки."""
    from suppliers.services import qr as qr_svc
    field, orig = ("image_back", "image_back_original") if side == "back" else ("image", "image_original")
    other = {getattr(m, f).name for f in (("image", "image_original") if side == "back" else
                                          ("image_back", "image_back_original")) if getattr(m, f)}
    old = [getattr(m, f) for f in (field, orig) if getattr(m, f)]
    old_names = [(f.storage, f.name) for f in old]
    getattr(m, field).save(f"{side}.jpg", upload, save=False)
    setattr(m, orig, "")
    updates = [field, orig]
    qr = str(qr or "").strip()
    if qr and qr not in (m.qr_text or ""):
        m.qr_text = qr_svc.join(m.qr_text or "", qr)
        updates.append("qr_text")
    m.save(update_fields=updates)
    new = getattr(m, field).name
    for storage, name in old_names:
        if name != new and name not in other:          # той самий файл в іншому полі не чіпаємо
            try:
                storage.delete(name)
            except Exception:  # noqa: BLE001
                pass


def drop_back(m):
    """🗑 Зворотний бік — геть (фото й оригінал до правок); лицьовий лишається."""
    keep = {f.name for f in (m.image, m.image_original) if f}
    for f in (m.image_back, m.image_back_original):
        _drop_file(f, keep)
    m.image_back, m.image_back_original = "", ""
    m.save(update_fields=["image_back", "image_back_original"])


def rotate(photo, degrees=90):
    """⟳ Повернути ще фото на 90° (за годинниковою стрілкою; −90 — проти) — новий файл JPEG."""
    from django.core.files.base import ContentFile
    from PIL import Image, ImageOps
    with photo.image.open("rb") as fh:
        img = ImageOps.exif_transpose(Image.open(fh)).convert("RGB")
    img = img.rotate(-int(degrees), expand=True)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    old = photo.image
    storage, name = old.storage, old.name
    photo.image.save("rotated.jpg", ContentFile(out.getvalue()), save=False)
    photo.save(update_fields=["image"])
    if name != photo.image.name:
        try:
            storage.delete(name)
        except Exception:  # noqa: BLE001
            pass


def files(m) -> list:
    """Усі фото групи для листа / документів клієнта: [(FieldFile, суфікс назви)] — «», «-back», «-2-flyer» …"""
    out = [(f, side) for f, side in ((m.image, ""), (m.image_back, "-back")) if f]
    for n, p in enumerate(m.photos.all(), 2):
        if p.image:
            out.append((p.image, f"-{n}-{p.kind}"))
    return out


def for_ai(m) -> list:
    """Фото групи для AI → [(підпис, FieldFile)]: боки першими, далі ще фото (до AI_PHOTOS разом)."""
    from suppliers.models import SupplierMedia
    what = {SupplierMedia.CARD: "візитка", SupplierMedia.FLYER: "флаєр / каталог"}.get(m.kind, "фото")
    out = []
    if m.image:
        out.append((f"{what}, лицьовий бік" if m.image_back or m.kind == SupplierMedia.CARD else what, m.image))
    if m.image_back:
        out.append((f"{what}, зворотний бік", m.image_back))
    for p in m.photos.all():
        if len(out) >= AI_PHOTOS:
            break
        if p.image:
            out.append((EXTRA_LABELS.get(p.kind, "інше фото") + (f" — {p.note}" if p.note else ""), p.image))
    return [(f"Фото {n} — {label}", f) for n, (label, f) in enumerate(out, 1)]


def has_more_cards(m) -> bool:
    return m.photos.filter(kind="card").exists()


def clean_people(value) -> list:
    """Люди з інших візиток групи (відповідь AI) → [{name, position, email, phone, mobile}] — лише рядки."""
    from suppliers.services import match
    out = []
    if not isinstance(value, list):
        return out
    for p in value[:10]:
        if not isinstance(p, dict):
            continue
        name = " ".join(str(p.get("name") or "").split())[:200]
        email = match.addr(p.get("email"))
        if not (name or email):
            continue
        out.append({"name": name, "position": " ".join(str(p.get("position") or "").split())[:200], "email": email,
                    "phone": str(p.get("phone") or "").strip()[:60], "mobile": str(p.get("mobile") or "").strip()[:80]})
    return out


def people(m) -> list:
    return clean_people((m.extracted or {}).get("people"))


def add_people(supplier, m, user=None) -> int:
    """👥 Люди з інших візиток групи → контактні особи постачальника (той самий email / ім'я — дописати порожнє).
    → скільки нових."""
    from suppliers.services import draft
    n = 0
    for p in people(m):
        _, created = draft.add_contact(supplier, {"contact_name": p["name"], "contact_position": p["position"],
                                                  "contact_email": p["email"], "contact_phone": p["phone"],
                                                  "contact_mobile": p["mobile"]},
                                       source="card", user=user, media=m)
        n += int(bool(created))
    return n
