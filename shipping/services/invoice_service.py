"""
shipping/services/invoice_service.py — Invoice generation service for Sevskiy GmbH.
Uses generate_invoice.py (stdlib only) + DigiKey Marketplace Orders API.
"""
import re
import logging
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_ORDERS_API_BASE = "/Sales/Marketplace2/Orders/v1"


def convert_docx_to_pdf(docx_path: Path, output_dir: Path) -> Path | None:
    """
    Convert .docx → .pdf via LibreOffice headless.
    Returns Path to the generated PDF, or None on failure.
    libreoffice-writer must be installed in the container (it is — see Dockerfile).
    """
    import subprocess
    try:
        result = subprocess.run(
            [
                "libreoffice", "--headless",
                "--convert-to", "pdf",
                "--outdir", str(output_dir),
                str(docx_path),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            pdf_path = output_dir / (docx_path.stem + ".pdf")
            if pdf_path.exists():
                logger.debug("PDF generated: %s", pdf_path)
                return pdf_path
        logger.warning("LibreOffice exit %s: %s", result.returncode, result.stderr[:200])
    except FileNotFoundError:
        logger.warning("libreoffice not found in PATH — PDF generation skipped")
    except Exception as e:
        logger.warning("convert_docx_to_pdf error: %s", e)
    return None


def get_next_invoice_number() -> str:
    """Auto-increment invoice number: max(DB, files in MEDIA/invoices/) + 1."""
    from shipping.models import Invoice

    db_max = 0
    try:
        last = Invoice.objects.order_by("-invoice_number").first()
        if last:
            db_max = int(re.sub(r"\D", "", last.invoice_number) or 0)
    except Exception:
        pass

    file_max = 0
    output_dir = Path(settings.MEDIA_ROOT) / "invoices"
    if output_dir.exists():
        for f in output_dir.glob("Invoice_*.docx"):
            m = re.search(r"Invoice_(\d+)\.docx", f.name)
            if m:
                file_max = max(file_max, int(m.group(1)))

    return str(max(db_max, file_max) + 1)


def _fmt_date(dt_str: str | None) -> str:
    """ISO 8601 datetime string → MM/DD/YYYY for the invoice template."""
    if not dt_str:
        return date.today().strftime("%m/%d/%Y")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return date.today().strftime("%m/%d/%Y")


def _fetch_digikey_order(order_id: str) -> dict:
    """
    Fetch order from DigiKey Marketplace Supplier API.
    Base: https://api.digikey.com/Sales/Marketplace2/Orders/v1
    Auth: 3-legged OAuth (marketplace_access_token stored in DigiKeyConfig).
    Returns order_dict compatible with generate_invoice.generate().

    TODO: The Marketplace Orders API requires 3-legged OAuth.
          If marketplace_access_token is missing, raises ValueError so the caller
          can fall back to manual form entry.
    """
    try:
        import requests
        from bots.models import DigiKeyConfig
        from bots.services.dk_marketplace import get_marketplace_token, _base_url, _headers
    except ImportError:
        raise ValueError("bots module not available")

    cfg = DigiKeyConfig.get()
    if not cfg.marketplace_access_token and not cfg.marketplace_refresh_token:
        raise ValueError("Marketplace OAuth token missing — authorize in DigiKey settings first.")

    token = get_marketplace_token(cfg)
    base = _base_url(cfg)
    url = f"{base}{_ORDERS_API_BASE}/orders"

    resp = requests.get(
        url,
        params={"BusinessIds": order_id, "Max": 1},
        headers=_headers(cfg, token),
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    orders = data.get("orders") or []
    if not orders:
        raise ValueError(f"Order {order_id!r} not found in DigiKey Marketplace.")

    o = orders[0]
    logger.debug("DK order raw: %s", o)
    cust = o.get("customer") or {}
    ship_addr = cust.get("shippingAddress") or {}

    # Contact name: firstName + lastName, fallback to name
    contact_parts = [ship_addr.get("firstName", ""), ship_addr.get("lastName", "")]
    contact = " ".join(p for p in contact_parts if p).strip() or ship_addr.get("name", "")

    # Address: street1, optionally append street2 on same line
    street1 = ship_addr.get("street1", "")
    street2 = ship_addr.get("street2", "")
    address1 = f"{street1}, {street2}".strip(", ") if street2 else street1

    # city_zip: "CITY, POSTALCODE" — if city empty try street3 / state as fallback
    city   = ship_addr.get("city", "") or ship_addr.get("street3", "") or ship_addr.get("state", "")
    postal = ship_addr.get("postalCode", "")
    if city and postal:
        city_zip = f"{city}, {postal}"
    elif city:
        city_zip = city
    else:
        city_zip = postal

    # VAT: Marketplace Orders API does not expose customer VAT — leave blank.
    # Do NOT read additionalFields (they contain numeric 0 values that become "0.0").
    vat_id = ""

    # Items: use productCategoryName as description (matches DigiKey product page)
    items = []
    for line in o.get("orderDetails") or []:
        qty = float(line.get("adjustedQuantity") or line.get("quantity") or 1)
        unit_price = float(line.get("unitPrice") or 0)
        part_no = line.get("supplierSku") or line.get("productPartNumber") or ""
        description = (
            line.get("productCategoryName")
            or line.get("productDescription")
            or line.get("offerDescription")
            or ""
        )
        items.append({
            "part_no": part_no,
            "description": description,
            "qty": qty,
            "unit_price": unit_price,
        })

    discount = float(o.get("adjustedTotalDiscountFee") or o.get("totalDiscountFee") or 0)
    shipping = float(o.get("adjustedShippingPrice") or o.get("shippingPrice") or 0)

    return {
        "digikey_order_no": o.get("businessId", order_id),
        "order_date":   _fmt_date(o.get("createDateUtc")),
        "shipment_date": _fmt_date(o.get("shippedDateUtc")),
        "items": items,
        "discount_amount":  -abs(discount) if discount else 0.0,
        "shipping_charges": shipping,
        "shipped_to": {
            "company":  ship_addr.get("companyName", ""),
            "contact":  contact,
            "address1": address1,
            "city_zip": city_zip,
            "country":  ship_addr.get("countryCode", ""),
            "vat_id":   vat_id,
        },
        # raw snapshot for debug (not used by generate())
        "_raw_shipping_address": ship_addr,
    }


def push_invoice_number_to_digikey(order_id: str, invoice_number: str) -> bool:
    """
    PATCH /Sales/Marketplace2/Orders/v1/orders/{id}/supplierInvoiceNumber
    Updates the supplierInvoiceNumber field on the DigiKey order.
    Returns True on success, False on any error (non-fatal).
    """
    try:
        import requests
        from bots.models import DigiKeyConfig
        from bots.services.dk_marketplace import get_marketplace_token, _base_url, _headers

        cfg = DigiKeyConfig.get()
        if not cfg.marketplace_access_token and not cfg.marketplace_refresh_token:
            return False
        token = get_marketplace_token(cfg)
        base = _base_url(cfg)

        # Find the order UUID (id) by businessId
        resp = requests.get(
            f"{base}{_ORDERS_API_BASE}/orders",
            params={"BusinessIds": order_id, "Max": 1},
            headers=_headers(cfg, token),
            timeout=15,
        )
        resp.raise_for_status()
        orders = resp.json().get("orders") or []
        if not orders:
            return False

        order_uuid = orders[0].get("id")
        if not order_uuid:
            return False

        patch_url = f"{base}{_ORDERS_API_BASE}/orders/{order_uuid}/supplierInvoiceNumber"
        patch_resp = requests.patch(
            patch_url,
            json={"supplierInvoiceNumber": invoice_number},
            headers=_headers(cfg, token),
            timeout=15,
        )
        patch_resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("push_invoice_number_to_digikey failed: %s", e)
        return False


class InvoiceService:

    TEMPLATE_PATH = Path(settings.BASE_DIR) / "shipping" / "templates_docx" / "invoice_template.docx"
    OUTPUT_DIR    = Path(settings.MEDIA_ROOT) / "invoices"

    @classmethod
    def generate_from_digikey_order(
        cls, dk_order_no: str, user,
        manual_data: dict | None = None,
        invoice_number: str | None = None,
        vat_id: str | None = None,
    ):
        """
        Generate an invoice from a DigiKey order number.
        - manual_data: skip API call, use this dict directly
        - invoice_number: override auto-generated number
        - vat_id: manually supplied VAT ID (API doesn't return it)
        Returns Invoice instance.
        """
        from shipping.models import Invoice
        from shipping.services.generate_invoice import generate, totals
        from decimal import Decimal

        if manual_data:
            order_data = manual_data
        else:
            order_data = _fetch_digikey_order(dk_order_no)

        if vat_id is not None:
            order_data = dict(order_data)
            order_data["shipped_to"] = dict(order_data.get("shipped_to") or {})
            order_data["shipped_to"]["vat_id"] = vat_id

        invoice_number = invoice_number or get_next_invoice_number()
        today = date.today().strftime("%m/%d/%Y")

        order_dict = {
            "invoice_number":   invoice_number,
            "invoice_date":     today,
            "digikey_order_no": order_data["digikey_order_no"],
            "order_date":       order_data["order_date"],
            "shipment_date":    order_data.get("shipment_date") or today,
            "items":            order_data["items"],
            "discount_amount":  float(order_data.get("discount_amount") or 0),
            "shipping_charges": float(order_data.get("shipping_charges") or 0),
            "shipped_to":       order_data["shipped_to"],
        }

        cls.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = cls.OUTPUT_DIR / f"Invoice_{invoice_number}.docx"
        generate(order_dict, output_path, cls.TEMPLATE_PATH)

        # Convert to PDF via LibreOffice (non-fatal if unavailable)
        pdf_path = convert_docx_to_pdf(output_path, cls.OUTPUT_DIR)

        T = totals(order_dict)

        # Link to existing SalesOrder by digikey_order_no if found
        sales_order = None
        try:
            from sales.models import SalesOrder
            sales_order = SalesOrder.objects.filter(
                order_number__icontains=order_data["digikey_order_no"]
            ).first()
        except Exception:
            pass

        inv = Invoice.objects.create(
            digikey_order_no   = order_data["digikey_order_no"],
            sales_order        = sales_order,
            invoice_number     = invoice_number,
            order_date         = _parse_date(order_data["order_date"]),
            shipment_date      = _parse_date(order_data.get("shipment_date")),
            subtotal           = T["sub"],
            discount_amount    = Decimal(str(order_data.get("discount_amount") or 0)),
            shipping_charges   = Decimal(str(order_data.get("shipping_charges") or 0)),
            vat_amount         = T["vat"],
            total_amount       = T["total"],
            shipped_to_company = order_data["shipped_to"].get("company", ""),
            shipped_to_vat     = order_data["shipped_to"].get("vat_id", ""),
            shipped_to_json    = order_data.get("shipped_to") or {},
            line_items         = order_data.get("items") or [],
            docx_file          = f"invoices/Invoice_{invoice_number}.docx",
            pdf_file           = f"invoices/Invoice_{invoice_number}.pdf" if pdf_path else None,
            created_by         = user,
        )

        return inv


def _parse_date(s: str | None):
    """MM/DD/YYYY or ISO → date object, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y").date()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        return None
