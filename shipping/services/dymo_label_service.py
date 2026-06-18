"""
DYMO label generator for RF coaxial cables.

Reads cable_label_template.dymo, substitutes variable fields via str.replace(),
writes the result to LABELS_DIR (settings.LABELS_DIR / media/labels/).

Template placeholders (exact strings in the XML):
  'Qty: 112 PCS '                              ← qty line (trailing space preserved)
  'Cable type: micro coax 1.13mm, length: 175mm'
  'Compatible with: U.FL-1st end, MHF4-2nd end'
  'CA-MHF1-MC1.13-175-MHF4'                   ← appears in P/N text AND barcode DataString
"""

import re
from pathlib import Path

from django.conf import settings


CONNECTOR_MAP = {
    'MHF1': 'U.FL',
    'MHF4': 'MHF4',
    'UMCC': 'UMCC',
    'SMA':  'SMA',
}

_CABLE_RE = re.compile(
    r'^CA-([A-Z0-9]+)-MC(\d+\.\d+)-(\d+)-([A-Z0-9]+)$',
    re.IGNORECASE,
)


def parse_part_number(part_no: str) -> dict:
    """
    'CA-MHF1-MC1.13-175-MHF4' → {part_no, coax_type, length, first_end, second_end}
    Raises ValueError if format doesn't match.
    """
    m = _CABLE_RE.match(part_no.strip())
    if not m:
        raise ValueError(f"Cannot parse part number: {part_no!r}")
    first_raw, coax_dia, length_mm, second_raw = m.groups()
    return {
        'part_no':    part_no,
        'coax_type':  f'{coax_dia}mm',
        'length':     f'{length_mm}mm',
        'first_end':  CONNECTOR_MAP.get(first_raw.upper(), first_raw),
        'second_end': CONNECTOR_MAP.get(second_raw.upper(), second_raw),
    }


def label_lines(parsed: dict, qty: int) -> dict:
    """Build the text strings that go on the label."""
    return {
        'qty_text': f'Qty: {qty} PCS ',   # trailing space matches template XML
        'cable':    f'Cable type: micro coax {parsed["coax_type"]}, length: {parsed["length"]}',
        'compat':   f'Compatible with: {parsed["first_end"]}-1st end, {parsed["second_end"]}-2nd end',
        'pn':       parsed['part_no'],
    }


class DymoLabelService:

    TEMPLATE_PATH = (
        Path(settings.BASE_DIR) / 'shipping' / 'templates_dymo' / 'cable_label_template.dymo'
    )

    # (placeholder_in_template, key_in_label_lines_dict)
    # Order matters: 'CA-MHF1-MC1.13-175-MHF4' must come AFTER the P/N line is already replaced,
    # but since our P/N replacement uses 'P/N: CA-...' → 'P/N: {pn}' we keep a single
    # bare-part-number replacement that handles both <Text>P/N: …</Text> and <DataString>…</DataString>.
    _REPLACEMENTS = [
        ('Qty: 112 PCS ',                                        'qty_text'),
        ('Cable type: micro coax 1.13mm, length: 175mm',         'cable'),
        ('Compatible with: U.FL-1st end, MHF4-2nd end',          'compat'),
        ('CA-MHF1-MC1.13-175-MHF4',                              'pn'),
    ]

    @classmethod
    def generate(cls, part_no: str, qty: int, output_path: Path = None) -> Path:
        """
        Generate a .dymo label file for one cable part number.

        Reads template, substitutes all variable fields, saves to LABELS_DIR.
        Returns the Path of the saved file.
        """
        parsed = parse_part_number(part_no)
        lines  = label_lines(parsed, qty)

        xml = cls.TEMPLATE_PATH.read_text(encoding='utf-8')
        for placeholder, key in cls._REPLACEMENTS:
            xml = xml.replace(placeholder, lines[key])

        labels_dir = Path(getattr(settings, 'LABELS_DIR', Path(settings.BASE_DIR) / 'labels'))
        labels_dir.mkdir(parents=True, exist_ok=True)

        safe_name = part_no.replace('/', '_').replace('\\', '_')
        out = output_path or (labels_dir / f'{safe_name}.dymo')
        out.write_text(xml, encoding='utf-8')
        return out

    @classmethod
    def generate_for_order(cls, order_items: list) -> list:
        """
        Generate labels for all CA-* positions in an order.

        order_items: [{'part_no': 'CA-MHF1-MC1.13-175-MHF4', 'qty': 20}, ...]
        Returns:     [{'part_no', 'qty', 'download_url'} | {'part_no', 'qty', 'error'}, ...]
        Silently skips items whose part_no doesn't start with 'CA-'.
        """
        results = []
        for item in order_items:
            part_no = (item.get('part_no') or '').strip()
            qty     = int(item.get('qty') or 1)
            if not part_no.upper().startswith('CA-'):
                continue
            try:
                path = cls.generate(part_no, qty)
                sku  = path.stem
                results.append({
                    'part_no':      part_no,
                    'qty':          qty,
                    'label_path':   str(path),
                    'download_url': f'/labels/serve/{sku}/?qty={qty}',
                })
            except Exception as exc:
                results.append({'part_no': part_no, 'qty': qty, 'error': str(exc)})
        return results
