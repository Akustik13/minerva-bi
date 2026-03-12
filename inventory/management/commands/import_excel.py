import re
import pandas as pd
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from inventory.models import Product, ProductAlias, Location, InventoryTransaction
from sales.models import SalesOrder, SalesOrderLine

def norm_col(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())

def find_col(cols, candidates):
    cols_norm = {norm_col(c): c for c in cols}
    for cand in candidates:
        cn = norm_col(cand)
        if cn in cols_norm:
            return cols_norm[cn]
    # fuzzy: contains
    for cand in candidates:
        cn = norm_col(cand)
        for k, orig in cols_norm.items():
            if cn in k:
                return orig
    return None

def clean_sku(x):
    """Normalize SKU / cell values coming from Excel."""
    if x is None:
        return ""
    # pandas / numpy NaN
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s.replace("\u200b", "")

def make_key(*parts):
    import hashlib
    raw = "|".join([str(p) for p in parts])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]

class Command(BaseCommand):
    help = "Import initial stock and sales from Excel files. Idempotent by external_key."

    def add_arguments(self, parser):
        parser.add_argument("--post", type=str, default="")
        parser.add_argument("--stock-ant", type=str, default="")
        parser.add_argument("--stock-cables", type=str, default="")
        parser.add_argument("--location", type=str, default="MAIN")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--stock-filters", default="", help="Path to filters stock Excel (ID FULL or ID short, Available Stock REAL)")


    def handle(self, *args, **opts):
        loc_code = opts["location"]
        location, _ = Location.objects.get_or_create(code=loc_code, defaults={"name": loc_code})

        # Ensure dev bootstrap (admin user + MAIN location) is available
        # (You can remove this later)
        from django.core.management import call_command
        call_command("bootstrap")

        if opts["stock_ant"]:
            self.import_stock(opts["stock_ant"], category="antenna", location=location, dry_run=opts["dry_run"])
        if opts["stock_cables"]:
            self.import_stock(opts["stock_cables"], category="cable", location=location, dry_run=opts["dry_run"])
        if opts.get("stock_filters"):
            self.import_stock(opts["stock_filters"], category="filter", location=location, dry_run=opts["dry_run"])
    
        if opts["post"]:
            # DigiKey — завжди продаж
            self.import_sales_post(
                opts["post"],
                sheet="digikey",
                source="digikey",
                location=location,
                dry_run=opts["dry_run"],
            )

            # NovaPost — відправка (за замовчуванням НЕ мінусує склад)
            self.import_sales_post(
                opts["post"],
                sheet="NovaPost",
                source="nova_post",
                location=location,
                dry_run=opts["dry_run"],
            )

            # Other — не факт що продаж
            self.import_sales_post(
                opts["post"],
                sheet="Other",
                source="other",
                location=location,
                dry_run=opts["dry_run"],
            )


        self.stdout.write(self.style.SUCCESS("Done."))

    def import_stock(self, path, category, location, dry_run=False):
        self.stdout.write(f"\n[STOCK] Reading: {path} (category={category})")
        # Try default sheet names first; fall back to first sheet
        xls = pd.ExcelFile(path)
        sheet = None
        for s in xls.sheet_names:
            if norm_col(s).startswith("parameters"):
                sheet = s
                break
        if sheet is None:
            sheet = xls.sheet_names[0]

        # --- auto-detect header row (Excel often has notes/merged cells on top) ---
        preview = pd.read_excel(path, sheet_name=sheet, header=None, nrows=60, dtype=object)

        def _cell_norm(v):
            return str(v).strip().lower() if v is not None else ""

        header_row = None
        for i in range(len(preview)):
            row_vals = [_cell_norm(x) for x in preview.iloc[i].tolist()]
            if (("id full" in row_vals) or ("id short" in row_vals)) and (any("available stock" in x for x in row_vals)):
                header_row = i
                break

        if header_row is None:
            # fallback: try to find at least "id full"
            for i in range(len(preview)):
                row_vals = [_cell_norm(x) for x in preview.iloc[i].tolist()]
                if ("id full" in row_vals) or ("id short" in row_vals):
                    header_row = i
                    break

        if header_row is None:
            raise RuntimeError(f"Could not detect header row in {path} sheet '{sheet}'. Please check where 'ID FULL' is located.")

        df = pd.read_excel(path, sheet_name=sheet, header=header_row, dtype=object)
        df = df.loc[:, ~df.columns.astype(str).str.contains("^Unnamed")]  # drop Unnamed columns
        # --- end auto-detect header row ---

        
        sku_col = find_col(df.columns, ["ID FULL", "ID_FULL", "sku", "product", "part number"]) or find_col(df.columns, ["ID short", "ID_SHORT"])
        qty_col = find_col(df.columns, ["Available Stock REAL", "available stock", "stock real", "available"])
        short_col = find_col(df.columns, ["ID short", "ID_SHORT", "short"])

        if not sku_col or not qty_col:
            raise RuntimeError(f"Could not find required columns in {path}. Need ID FULL (or ID short) and Available Stock REAL. Found columns: {list(df.columns)}")

        created_tx = 0
        updated_products = 0

        with transaction.atomic():
            for _, row in df.iterrows():
                # Prefer ID FULL, fall back to ID short
                sku = clean_sku(row.get(sku_col)) if sku_col else ""
                if not sku and short_col:
                    sku = clean_sku(row.get(short_col))
                if not sku:
                    continue
                raw_qty = row.get(qty_col) if qty_col else None
                try:
                    qty = Decimal(str(raw_qty).replace(",", ".").strip()) if str(raw_qty).strip() not in ("", "nan", "None") else Decimal("0")
                except Exception:
                    qty = Decimal("0")

                sku_short = clean_sku(row.get(short_col)) if short_col else ""
                product, created = Product.objects.get_or_create(sku=sku, defaults={"category": category, "sku_short": sku_short})
                if not created:
                    # keep category if already set, but fill missing sku_short
                    changed = False
                    if product.category == "other" and category != "other":
                        product.category = category
                        changed = True
                    if sku_short and not product.sku_short:
                        product.sku_short = sku_short
                        changed = True
                    if changed:
                        product.save(update_fields=["category", "sku_short"])
                        updated_products += 1

                if sku_short:
                    ProductAlias.objects.get_or_create(alias=sku_short, defaults={"product": product})

                ext_key = make_key("INITIAL", category, sku, str(qty), "from_excel", path)
                if InventoryTransaction.objects.filter(external_key=ext_key).exists():
                    continue
                if dry_run:
                    created_tx += 1
                    continue

                InventoryTransaction.objects.create(
                    external_key=ext_key,
                    product=product,
                    location=location,
                    tx_type="INITIAL",
                    qty=qty,
                    ref_doc=f"initial:{category}",
                    tx_date=timezone.now(),
                )
                created_tx += 1

        self.stdout.write(self.style.SUCCESS(f"[STOCK] Imported INITIAL tx: {created_tx}, products updated: {updated_products}"))

    def resolve_product(self, sku_raw: str):
        sku_raw = clean_sku(sku_raw)
        if not sku_raw:
            return None, sku_raw

        # direct match
        p = Product.objects.filter(sku=sku_raw).first()
        if p:
            return p, sku_raw

        # alias match
        alias = ProductAlias.objects.filter(alias=sku_raw).select_related("product").first()
        if alias:
            return alias.product, sku_raw

        # create placeholder product (unknown)
        p, _ = Product.objects.get_or_create(sku=sku_raw, defaults={"category": "other"})
        return p, sku_raw

    def import_sales_post(self, path, sheet="digikey", source="digikey", location=None, dry_run=False):
        # --- defaults by source ---
        if source == "digikey":
            default_doc_type = "SALE"
            default_affects = True
        elif source == "nova_post":
            default_doc_type = "TRANSFER"
            default_affects = False
        else:  # other
            default_doc_type = "OTHER"
            default_affects = False

        self.stdout.write(f"\n[SALES] Reading: {path} sheet={sheet} source={source}")
        df = pd.read_excel(path, sheet_name=sheet, dtype=object)

        sku_col = find_col(df.columns, ["product namber", "product number", "part number", "pn"])
        qty_col = find_col(df.columns, ["QTY", "qty", "quantity"])
        order_col = find_col(df.columns, ["Sales Order", "sales order", "order"])
        order_date_col = find_col(df.columns, ["Order Date", "order date", "date"])
        ship_date_col = find_col(df.columns, ["Shipping", "shipping"])
        courier_col = find_col(df.columns, ["Shipping Courier", "courier"])
        tracking_col = find_col(df.columns, ["tracking number", "tracking"])
        client_col = find_col(df.columns, ["client"])
        email_col = find_col(df.columns, ["Email", "email"])
        addr_col = find_col(df.columns, ["Shipping Address", "address"])
        region_col = find_col(df.columns, ["Shipping Region", "region"])
        lief_col = find_col(df.columns, ["Lieferschein-Nr", "lieferschein"])
        deadline_col = find_col(df.columns, ["Shipping Deadline", "deadline"])


        if not sku_col or not qty_col:
            raise RuntimeError("Post.xlsx: product namber або QTY не знайдено")

        last_order_number = None
        last_order_date = None
        last_ship_date = None
        last_courier = ""
        last_tracking = ""
        last_client = ""
        last_email = ""
        last_addr = ""
        last_region = ""
        last_lief = ""
        last_deadline = None


        created_orders = 0
        created_lines = 0
        created_tx = 0
        unknown = set()

        with transaction.atomic():
            for idx, row in df.iterrows():
                # --- QTY ---
                raw_qty = row.get(qty_col) if qty_col else None

                # skip empty/NaN
                if raw_qty is None or (isinstance(raw_qty, float) and pd.isna(raw_qty)) or str(raw_qty).strip().lower() in ("", "nan", "none", "-", "—"):
                    continue

                try:
                    qty = Decimal(str(raw_qty).replace(",", ".").strip())
                except Exception:
                    continue

                # protect against NaN/Infinity decimals
                if not qty.is_finite():
                    continue

                if qty <= 0:
                    continue


                # --- SKU ---
                sku_raw = clean_sku(row.get(sku_col)) if sku_col else ""
                if not sku_raw:
                    continue

                # --- Sales Order (inherit if empty) ---
                order_number = clean_sku(row.get(order_col)) if order_col else ""
                if order_number:
                    last_order_number = order_number
                else:
                    order_number = last_order_number

                if not order_number:
                    continue  # no context at all → skip

                # --- Order Date (inherit if empty) ---
                od_raw = row.get(order_date_col)
                if pd.notna(od_raw):
                    try:
                        last_order_date = timezone.make_aware(
                            pd.to_datetime(od_raw).to_pydatetime(),
                            timezone.get_current_timezone()
                        )
                    except Exception:
                        pass

                order_date = last_order_date

                # --- Product resolve ---
                product, _ = self.resolve_product(sku_raw)
                if product.category == "other":
                    unknown.add(product.sku)

                # --- Sales Order ---
                so, so_created = SalesOrder.objects.get_or_create(
                    source=source,
                    order_number=order_number,
                    defaults={
                        "order_date": order_date,
                        "document_type": default_doc_type,
                        "affects_stock": default_affects,
                    },
                )

                
                def _inherit_str(val, last):
                    if val is None:
                        return last
                    s = str(val).strip()
                    if s == "" or s.lower() in ("nan", "none", "-", "—"):
                        return last
                    return s

                def _inherit_dt(val, last):
                    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip().lower() in ("", "nan", "none", "-", "—"):
                        return last
                    try:
                        d = pd.to_datetime(val, errors="coerce")
                        if pd.isna(d):
                            return last
                        return timezone.make_aware(d.to_pydatetime(), timezone.get_current_timezone())
                    except Exception:
                        return last

                def _inherit_date(val, last):
                    if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip().lower() in ("", "nan", "none", "-", "—"):
                        return last
                    try:
                        d = pd.to_datetime(val, errors="coerce")
                        if pd.isna(d):
                            return last
                        return d.date()
                    except Exception:
                        return last

                # inherit values
                last_ship_date = _inherit_dt(row.get(ship_date_col) if ship_date_col else None, last_ship_date)
                last_courier   = _inherit_str(row.get(courier_col) if courier_col else None, last_courier)
                last_tracking  = _inherit_str(row.get(tracking_col) if tracking_col else None, last_tracking)
                last_client    = _inherit_str(row.get(client_col) if client_col else None, last_client)
                last_email     = _inherit_str(row.get(email_col) if email_col else None, last_email)
                last_addr      = _inherit_str(row.get(addr_col) if addr_col else None, last_addr)
                last_region    = _inherit_str(row.get(region_col) if region_col else None, last_region)
                last_lief      = _inherit_str(row.get(lief_col) if lief_col else None, last_lief)
                last_deadline  = _inherit_date(row.get(deadline_col) if deadline_col else None, last_deadline)

                # update SalesOrder (only fill if empty)
                changed = False
                if last_ship_date and so.shipped_at is None:
                    so.shipped_at = last_ship_date; changed = True
                if last_courier and not so.shipping_courier:
                    so.shipping_courier = last_courier; changed = True
                if last_tracking and not so.tracking_number:
                    so.tracking_number = last_tracking; changed = True
                if last_client and not so.client:
                    so.client = last_client; changed = True
                if last_email and not so.email:
                    so.email = last_email; changed = True
                if last_addr and not so.shipping_address:
                    so.shipping_address = last_addr; changed = True
                if last_region and not so.shipping_region:
                    so.shipping_region = last_region; changed = True
                if last_lief and not so.lieferschein_nr:
                    so.lieferschein_nr = last_lief; changed = True
                if last_deadline and so.shipping_deadline is None:
                    so.shipping_deadline = last_deadline; changed = True

                if changed:
                    so.save()

                
                if so_created:
                    created_orders += 1

                # --- Sales Order Line (idempotent) ---
                line_key = make_key("LINE", source, order_number, sku_raw, qty)
                if not SalesOrderLine.objects.filter(
                    order=so, product=product, qty=qty
                ).exists():
                    SalesOrderLine.objects.create(
                        order=so,
                        product=product,
                        sku_raw=sku_raw,
                        qty=qty,
                    )
                    created_lines += 1

                # --- Inventory Transaction SALE ---
                tx_key = make_key("SALE", source, order_number, sku_raw, qty)
                if InventoryTransaction.objects.filter(external_key=tx_key).exists():
                    continue

                if so.affects_stock:
                    InventoryTransaction.objects.create(
                        external_key=tx_key,
                        product=product,
                        location=location,
                        tx_type="SALE",
                        qty=-qty,
                        ref_doc=f"{source}:{order_number}",
                        tx_date=order_date or timezone.now(),
                    )
                    created_tx += 1


        self.stdout.write(self.style.SUCCESS(
            f"[SALES] Orders: {created_orders}, lines: {created_lines}, SALE tx: {created_tx}"
        ))

        if unknown:
            self.stdout.write(self.style.WARNING(
                "Unknown SKUs (need alias mapping):\n  - " + "\n  - ".join(sorted(unknown))
            ))
