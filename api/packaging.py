"""REST API упаковки: коробки (пакувальні матеріали), їхній запас, використання, схожі розміри й об'єднання.

    GET   /packaging/                  — коробки (active=true|false|all, q — назва / код, box_type, low=true;
                                         with_usage=1 — ще й використання за 365 днів)
    GET   /packaging/{id}/             — одна коробка (with_usage=1 — теж)
    POST  /packaging/                  — нова коробка
    PATCH /packaging/{id}/             — змінити (в архів — is_active=false); запас тут не змінюється
    POST  /packaging/{id}/receive/     — 📥 прийшло (купили) qty коробок
    POST  /packaging/{id}/count/       — 🧮 інвентаризація: фактично є actual
    GET   /packaging/usage/?days=365   — скільки коробок кожного виду пішло, розміри без коробки
    GET   /packaging/similar/?tolerance=1&inactive=0 — групи схожих розмірів і в яку зливати
    POST  /packaging/merge/            — злити коробки в одну {target, merge: [id…]}
    GET   /packaging/merges/?target=   — 🔀 історія злиттів: до злиття, зараз у цілі, після злиття, чи можна відкотити
    POST  /packaging/merges/{id}/undo/ — ↩ відкотити злиття (лише останнє для цих коробок)

Права ключа: packaging:read / packaging:write. Ключ сайту магазину — 403 shop_key_forbidden. DELETE — 405.
Логіка — shipping/services/packaging_stock.py (запас) і shipping/services/packaging_usage.py (використання, схожі,
злиття); тут лише перевірка запиту й відповідь.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from shipping.models import PackagingMaterial
from shipping.services import box_names, packaging_stock
from shipping.services import packaging_usage as pu

from .models import APIKey
from .views import NoDeleteMixin, _bound_shop

TRUE = ("1", "true", "yes", "on")
FALSE = ("0", "false", "no", "off")
QTY_MAX = 1_000_000
MERGE_MAX = 50
DIM_MIN = Decimal("0.1")
SHOP_DENIED = ("Ключ сайту магазину не має доступу до упаковки — потрібен окремий ключ з правом "
               "packaging:read / packaging:write.")


def _num(value):
    if value is None:
        return None
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


def _fail(detail, code, http=status.HTTP_400_BAD_REQUEST, **extra):
    return Response({"detail": detail, "code": code, **extra}, status=http)


def _who(request):
    """Від чийого імені рух / злиття в журналі: власник ключа, інакше людина агента ключа; інакше нікого."""
    key = request.auth if isinstance(request.auth, APIKey) else None
    if key is None:
        return None
    if key.owner_id:
        return key.owner
    if key.agent_user_id:
        return key.agent_user
    return None


def _note(request, note, default):
    key = request.auth if isinstance(request.auth, APIKey) else None
    label = f"API: {key.name}" if key else "API"
    return f"{(note or '').strip() or default} · {label}"


# ── Серіалізатори ─────────────────────────────────────────────────────────────

class FoldHeightsField(serializers.Field):
    """«Можна скласти до висоти»: відповідь — список чисел, запит — список [22, 16] або рядок «22, 16»."""
    default_error_messages = {"invalid": "Висоти — списком чисел, напр. [22, 16], або рядком «22, 16»."}

    def to_representation(self, value):
        good, _ = PackagingMaterial.parse_fold_heights(value)
        return [_num(h) for h in sorted(good, reverse=True)]

    def to_internal_value(self, data):
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        if isinstance(data, (list, tuple)):
            parts = []
            for v in data:
                if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                    self.fail("invalid")
                parts.append(str(v))
            return ", ".join(parts)
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            return str(data)
        self.fail("invalid")


class PackagingSerializer(serializers.ModelSerializer):
    """Коробка. stock_qty — лише читання (запас — /receive/, /count/, відправлення); min_stock — що діє (свій або
    «за замовчуванням» з налаштувань доставки), own_min_stock — свій (null — за замовчуванням)."""
    code = serializers.CharField(max_length=20, required=False, allow_blank=True,
                                 help_text="Код для наклейки й штрихкоду, напр. BX-M-02")
    fold_heights = FoldHeightsField(required=False)
    size_class_effective = serializers.SerializerMethodField()
    min_stock = serializers.SerializerMethodField()
    own_min_stock = serializers.IntegerField(source="min_stock", required=False, allow_null=True, min_value=0)
    is_low = serializers.SerializerMethodField()
    volume_cm3 = serializers.FloatField(read_only=True)

    class Meta:
        model = PackagingMaterial
        fields = ["id", "code", "name", "box_type", "size_class", "size_class_effective", "length_cm", "width_cm",
                  "height_cm", "fold_heights", "tare_weight_kg", "max_weight_kg", "cost", "is_active", "track_stock",
                  "stock_qty", "min_stock", "own_min_stock", "reorder_qty", "is_low", "volume_cm3", "buy_url", "notes",
                  "merged_into"]
        read_only_fields = ["id", "stock_qty", "merged_into"]
        extra_kwargs = {"length_cm": {"min_value": DIM_MIN}, "width_cm": {"min_value": DIM_MIN},
                        "height_cm": {"min_value": DIM_MIN}, "tare_weight_kg": {"min_value": Decimal("0")},
                        "max_weight_kg": {"min_value": Decimal("0")}, "cost": {"min_value": Decimal("0")}}

    def get_size_class_effective(self, obj):
        return box_names.class_of(obj)

    def get_min_stock(self, obj):
        return obj.effective_min

    def get_is_low(self, obj):
        return obj.is_low

    def validate_code(self, value):
        code = box_names.clean_code(value)
        if not code:
            return ""
        if not box_names.CODE_RE.match(code):
            raise serializers.ValidationError("Код — латинські великі літери, цифри й дефіс, до 20 символів "
                                              "(напр. BX-M-02).")
        other = PackagingMaterial.objects.filter(code=code)
        if self.instance is not None:
            other = other.exclude(pk=self.instance.pk)
        other = other.first()
        if other is not None:
            raise serializers.ValidationError(f"Код {code} уже має коробка #{other.pk} «{other}».")
        return code

    def validate(self, attrs):
        """Як PackagingMaterial.clean (висоти складання), на копії — сам запис не змінюється до збереження."""
        probe = copy.copy(self.instance) if self.instance is not None else PackagingMaterial()
        for name, value in attrs.items():
            setattr(probe, name, value)
        try:
            probe.clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(e.message_dict if hasattr(e, "error_dict") else
                                              {"non_field_errors": e.messages})
        if "fold_heights" in attrs or "height_cm" in attrs:
            if len(probe.fold_heights) > PackagingMaterial._meta.get_field("fold_heights").max_length:
                raise serializers.ValidationError({"fold_heights": "Забагато висот."})
            attrs["fold_heights"] = probe.fold_heights
        return attrs

    def update(self, instance, validated_data):
        """Лише змінені поля (+ ті, що ставить save()): запас і «попередили мало» не перезаписуються застарілим."""
        for name, value in validated_data.items():
            setattr(instance, name, value)
        instance.save(update_fields=sorted(set(validated_data) | {"code", "name", "stock_since"}))
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        stats = self.context.get("usage")
        if stats is not None:
            st = stats.get(instance.pk) or {}
            data["usage"] = {k: st.get(k) or 0 for k in ("boxes", "shipments", "recs", "orders")}
            data["usage"]["last"] = st.get("last")
            data["usage"]["days"] = self.context.get("usage_days")
        return data


class ReceiveSerializer(serializers.Serializer):
    qty = serializers.IntegerField(min_value=1, max_value=QTY_MAX, help_text="Скільки коробок прийшло")
    note = serializers.CharField(required=False, allow_blank=True, max_length=200,
                                 help_text="Напр. номер рахунку постачальника")


class CountSerializer(serializers.Serializer):
    actual = serializers.IntegerField(min_value=0, max_value=QTY_MAX, help_text="Скільки коробок фактично є")
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)


class MergeSerializer(serializers.Serializer):
    target = serializers.IntegerField(help_text="id коробки, у яку зливати")
    merge = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=MERGE_MAX,
                                  help_text="id коробок, які злити в target")


class NoBodySerializer(serializers.Serializer):
    """Звіти /usage/ і /similar/ — без серіалізатора моделі (поля — в описі)."""


def _movement(mv):
    if mv is None:
        return None
    return {"id": mv.pk, "kind": mv.kind, "kind_label": mv.get_kind_display(), "qty": mv.qty,
            "balance": mv.balance, "note": mv.note, "created_at": mv.created_at}


def _box(m, st=None) -> dict:
    st = st or {}
    return {"id": m.pk, "code": m.code, "name": str(m), "dims": pu.dims_text(m), "is_active": m.is_active,
            "boxes": st.get("boxes", 0), "merged_boxes": st.get("merged", 0), "shipments": st.get("shipments", 0),
            "last": st.get("last"), "recs": st.get("recs", 0), "orders": st.get("orders", 0)}


def _history_row(h) -> dict:
    m = h["merge"]
    return {"id": m.pk, "created_at": m.created_at, "user": getattr(h["user"], "username", None), "via": m.via,
            "target_id": m.target_id, "box_ids": [b.pk for b in m.boxes.all()],
            "before": h["before"], "total_before": h["total"], "target_now": h["now"], "after_merge": h["after"],
            "result": m.result, "undone_at": m.undone_at, "undone_by": getattr(h["undone_by"], "username", None),
            "can_undo": h["can_undo"], "blocked_by": h["blocked_by"].pk if h["blocked_by"] else None}


# ── Адреси ────────────────────────────────────────────────────────────────────

class PackagingViewSet(NoDeleteMixin, viewsets.GenericViewSet):
    """Коробки (пакувальні матеріали): каталог, запас, використання, схожі розміри, об'єднання."""
    resource_scope = "packaging"
    queryset = PackagingMaterial.objects.all()           # сесія адмінки: право shipping.view_packagingmaterial
    serializer_class = PackagingSerializer
    filter_backends = []
    lookup_value_regex = r"\d+"
    shop_key_forbidden = True                           # api/key_insight._access: ключ магазину — «заборонено»
    shop_denied_text = "ключ сайту магазину не має доступу до упаковки"

    def check_permissions(self, request):
        if _bound_shop(request) is not None:
            self.permission_denied(request, message={"detail": SHOP_DENIED, "code": "shop_key_forbidden"})
        super().check_permissions(request)

    def get_queryset(self):
        return PackagingMaterial.objects.order_by("box_type", "length_cm", "width_cm", "height_cm", "pk")

    def get_serializer_class(self):
        return {"receive": ReceiveSerializer, "count": CountSerializer, "merge": MergeSerializer,
                "usage": NoBodySerializer, "similar": NoBodySerializer}.get(self.action, PackagingSerializer)

    def _context(self, request) -> dict:
        ctx = self.get_serializer_context()
        if str(request.query_params.get("with_usage", "")).lower() in TRUE:
            data = pu.usage()                                   # один раз на запит, не на кожну коробку
            ctx["usage"], ctx["usage_days"] = data["materials"], data["days"]
        return ctx

    def _data(self, obj, ctx=None):
        return PackagingSerializer(obj, context=ctx if ctx is not None else self.get_serializer_context()).data

    # ── Читання ──

    def list(self, request):
        """Коробки (active=true|false|all, q, box_type, low=true, with_usage=1)."""
        p = request.query_params
        qs = self.get_queryset()
        active = str(p.get("active") or "true").lower()
        if active in TRUE:
            qs = qs.filter(is_active=True)
        elif active in FALSE:
            qs = qs.filter(is_active=False)
        elif active != "all":
            return _fail("active — true, false або all.", "bad_param")
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))
        box_type = (p.get("box_type") or "").strip()
        if box_type:
            if box_type not in PackagingMaterial.BoxType.values:
                return _fail("box_type — один з: " + ", ".join(PackagingMaterial.BoxType.values) + ".", "bad_param")
            qs = qs.filter(box_type=box_type)
        low = str(p.get("low") or "").lower() in TRUE
        if low:
            qs = qs.filter(track_stock=True)
        items = packaging_stock._with_default(list(qs))        # «мінімум за замовчуванням» — один запит
        if low:
            items = [m for m in items if m.is_low]
        ctx = self._context(request)
        page = self.paginate_queryset(items)
        if page is not None:
            return self.get_paginated_response(PackagingSerializer(page, many=True, context=ctx).data)
        return Response(PackagingSerializer(items, many=True, context=ctx).data)

    def retrieve(self, request, pk=None):
        """Одна коробка (with_usage=1 — ще й використання за 365 днів)."""
        return Response(self._data(self.get_object(), ctx=self._context(request)))

    # ── Запис ──

    def _save(self, serializer):
        try:
            with transaction.atomic():
                return serializer.save(), None
        except IntegrityError:
            return None, _fail("Такий код уже має інша коробка.", "code_taken")

    def create(self, request):
        """Нова коробка (stock_qty не приймається — запас лише через /receive/ і /count/)."""
        s = PackagingSerializer(data=request.data, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        obj, error = self._save(s)
        if error is not None:
            return error
        return Response(self._data(obj), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        """Змінити поля коробки; в архів — is_active=false (stock_qty не змінюється)."""
        obj = self.get_object()
        before = (obj.min_stock, obj.is_active)
        s = PackagingSerializer(obj, data=request.data, partial=True, context=self.get_serializer_context())
        s.is_valid(raise_exception=True)
        obj, error = self._save(s)
        if error is not None:
            return error
        if (obj.min_stock, obj.is_active) != before:          # як картка коробки: «мало» — перевірити знову
            pk_ = obj.pk
            transaction.on_commit(lambda: packaging_stock.check_low(pk_))
        obj.refresh_from_db()
        return Response(self._data(obj))

    def _stock_action(self, request, serializer_class):
        obj = self.get_object()
        s = serializer_class(data=request.data)
        s.is_valid(raise_exception=True)
        if not obj.track_stock:
            return obj, s.validated_data, _fail(
                f"Коробка «{obj}» без обліку на складі — увімкніть «Вести облік на складі» (PATCH track_stock=true).",
                "no_stock_tracking")
        return obj, s.validated_data, None

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        """📥 Прийшло (купили) qty коробок — додати до запасу."""
        obj, data, error = self._stock_action(request, ReceiveSerializer)
        if error is not None:
            return error
        mv = packaging_stock.receive(obj, data["qty"], user=_who(request),
                                     note=_note(request, data.get("note"), "Прихід"))
        obj.refresh_from_db()
        return Response({"movement": _movement(mv), "packaging": self._data(obj)})

    @action(detail=True, methods=["post"])
    def count(self, request, pk=None):
        """🧮 Інвентаризація: фактично є actual коробок — рух на різницю."""
        obj, data, error = self._stock_action(request, CountSerializer)
        if error is not None:
            return error
        actual = data["actual"]
        mv = packaging_stock.count(obj, actual, user=_who(request),
                                   note=_note(request, data.get("note"), f"Інвентаризація: є {actual}"))
        obj.refresh_from_db()
        return Response({"changed": mv is not None, "movement": _movement(mv), "packaging": self._data(obj)})

    # ── Звіти й об'єднання ──

    @action(detail=False, methods=["get"])
    def usage(self, request):
        """📊 Скільки коробок кожного виду пішло за ?days= днів (оформлені відправлення від нас), розміри без коробки."""
        raw = request.query_params.get("days") or "365"
        try:
            days = int(raw)
        except (TypeError, ValueError):
            return _fail("days — ціле число днів.", "bad_param")
        if days < 1:
            return _fail("days — від 1.", "bad_param")
        data = pu.usage(days)
        stats = data["materials"]
        rows = [_box(m, stats.get(m.pk)) for m in PackagingMaterial.objects.order_by("pk")]
        rows.sort(key=lambda r: (-r["boxes"], -r["recs"], r["name"].lower()))
        return Response({"days": data["days"], "since": data["since"], "shipments": data["shipments"],
                         "materials": rows, "unmatched": data["unmatched"]})

    @action(detail=False, methods=["get"])
    def similar(self, request):
        """🔍 Групи коробок схожих розмірів (±tolerance см кожен вимір) і в яку зливати."""
        p = request.query_params
        try:
            tol = Decimal(str(p.get("tolerance") or "1").replace(",", "."))
        except InvalidOperation:
            return _fail("tolerance — число сантиметрів, напр. 1 або 0.5.", "bad_param")
        if not tol.is_finite() or tol < 0:
            return _fail("tolerance — число сантиметрів від 0.", "bad_param")
        tol = min(tol, pu.TOLERANCE_MAX)
        inactive = str(p.get("inactive") or "").lower() in TRUE
        stats = pu.usage()["materials"]
        groups = pu.similar(tol, include_inactive=inactive, stats=stats)
        out = [{"target_id": g["target"].pk,
                "members": [{k: v for k, v in _box(m, stats.get(m.pk)).items()
                             if k in ("id", "code", "name", "dims", "is_active", "boxes", "recs")}
                            for m in g["members"]],
                "spread": [_num(x) for x in g["spread"]]} for g in groups]
        return Response({"tolerance": _num(tol), "include_inactive": inactive, "groups": out})

    @action(detail=False, methods=["post"])
    def merge(self, request):
        """🔀 Злити коробки merge в target: рекомендації товарів, «упаковано» й запас — на ціль, злиті — в архів."""
        s = MergeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        target_id, ids = s.validated_data["target"], list(dict.fromkeys(s.validated_data["merge"]))
        target = PackagingMaterial.objects.filter(pk=target_id).first()
        if target is None:
            return _fail(f"Коробки #{target_id} немає.", "not_found", status.HTTP_404_NOT_FOUND)
        others = list(PackagingMaterial.objects.filter(pk__in=ids).order_by("pk"))
        missing = sorted(set(ids) - {m.pk for m in others})
        if missing:
            return _fail("Немає коробок: " + ", ".join(f"#{i}" for i in missing) + ".", "not_found",
                         status.HTTP_404_NOT_FOUND, missing=missing)
        try:
            out = pu.merge(target, others, user=_who(request), via="api")
        except pu.MergeError as e:
            return _fail(str(e), "merge_refused")
        target.refresh_from_db()
        return Response({**out, "target": self._data(target)})

    @action(detail=False, methods=["get"])
    def merges(self, request):
        """🔀 Історія злиттів (?target=id, ?limit= до 200): що злили, скільки кожна коробка мала до злиття, сума,
        зараз у цілі й після злиття, чи відкочено і чи можна відкотити."""
        p = request.query_params
        target = None
        if p.get("target"):
            target = PackagingMaterial.objects.filter(pk=p.get("target") if str(p.get("target")).isdigit() else 0).first()
            if target is None:
                return _fail("Такої коробки немає.", "not_found", status.HTTP_404_NOT_FOUND)
        try:
            limit = max(1, min(int(p.get("limit") or 50), 200))
        except ValueError:
            return _fail("limit — ціле число від 1 до 200.", "bad_param")
        return Response({"merges": [_history_row(h) for h in pu.history(target=target, limit=limit)]})

    @action(detail=False, methods=["post"], url_path=r"merges/(?P<merge_id>\d+)/undo")
    def merge_undo(self, request, merge_id=None):
        """↩ Відкотити злиття: коробки, рекомендації товарів, «упаковано» й запас — як до злиття."""
        from shipping.models import PackagingMerge
        m = PackagingMerge.objects.filter(pk=merge_id).first()
        if m is None:
            return _fail(f"Злиття #{merge_id} немає.", "not_found", status.HTTP_404_NOT_FOUND)
        try:
            out = pu.undo(m, user=_who(request))
        except pu.MergeError as e:
            return _fail(str(e), "undo_refused", status.HTTP_409_CONFLICT)
        return Response({"id": m.pk, "undone": True, **out})
