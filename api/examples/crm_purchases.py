"""
Приклад інтеграції зовнішньої CRM із складом Minerva: закупівлі в постачальників.

CRM замовляє товар у постачальника → передає замовлення в Minerva (склад бачить «Очікується»)
→ трек-номер і орієнтовна дата → «доставлено» → Minerva оприбутковує товар один раз.

Запуск (потрібен лише пакет requests):
    set MINERVA_URL=https://<ваш-домен>             (PowerShell: $env:MINERVA_URL="...")
    set MINERVA_TOKEN=<ключ з правами purchases:read + purchases:write>
    python api/examples/crm_purchases.py ping
    python api/examples/crm_purchases.py order B-2026-0042 AMP-100:10 CABLE-M:25
    python api/examples/crm_purchases.py get B-2026-0042
    python api/examples/crm_purchases.py list                  # ще очікуються (open=1)
    python api/examples/crm_purchases.py track B-2026-0042 1Z999AA10123456784 UPS 2026-11-05
    python api/examples/crm_purchases.py delivered B-2026-0042 LS-118 AMP-100:4
    python api/examples/crm_purchases.py delivered B-2026-0042   # уся решта
    python api/examples/crm_purchases.py cancel B-2026-0042 "Lieferant storniert"
    python api/examples/crm_purchases.py sync                  # що змінилось з минулого запуску
    python api/examples/crm_purchases.py demo AMP-100          # увесь цикл з поясненнями

Номер замовлення в CRM (external_id) — ключ проти дублів: той самий запит можна повторювати
(збій мережі, тайм-аут) — Minerva оновить те саме замовлення, а «доставлено» не оприбуткує двічі.
Ключ НЕ вставляйте в JavaScript браузера — лише в серверний код CRM.
"""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

# Windows-консоль (cp1251) не друкує ✓/→ — перемикаємо вивід на UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.getenv("MINERVA_URL", "http://localhost:8000").rstrip("/") + "/api/v1"
TOKEN = os.getenv("MINERVA_TOKEN", "")
SOURCE = os.getenv("CRM_SOURCE", "")  # порожньо — «Джерело замовлень» ключа (або crm)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_purchase_sync")  # для sync

session = requests.Session()
session.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})


def call(method, path, quiet=False, **kw):
    if SOURCE and method != "GET":
        kw["json"] = {"source": SOURCE, **kw.get("json", {})}
    elif SOURCE:
        kw["params"] = {"source": SOURCE, **kw.get("params", {})}
    url = path if path.startswith("http") else BASE + path  # або повна адреса (next у списку)
    for attempt in range(5):
        try:
            r = session.request(method, url, timeout=30, **kw)
        except requests.ConnectionError:
            # Мережа впала посеред запиту — повтор безпечний: дубля не буде
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:  # ліміт запитів — чекаємо і повторюємо
            time.sleep(int(r.headers.get("Retry-After", 5)))
            continue
        break
    else:
        sys.exit(f"{method} {path}: Minerva не відповідає")
    try:
        body = r.json()
    except ValueError:
        body = r.text
    if not quiet:
        print(f"{method} {path} → {r.status_code}")
        print(json.dumps(body, ensure_ascii=False, indent=2, default=str))
    return r.status_code, body


def ref(external_id):
    """Адреса закупівлі за номером у CRM: /purchase-orders/ext:<номер>/."""
    return "/purchase-orders/ext:" + quote(external_id, safe="") + "/"


def parse_lines(args):
    """AMP-100:10 CABLE-M:25 → позиції; line_id — номер рядка в CRM (тут — порядковий)."""
    lines = []
    for i, a in enumerate(args, 1):
        sku, _, qty = a.rpartition(":") if ":" in a else (a, "", "1")
        lines.append({"line_id": str(i), "sku": sku, "qty": float(qty)})
    return lines


def send_order(external_id, lines, quiet=False, **extra):
    """Замовлення постачальнику створено / змінено в CRM. Повтор = оновлення, не дубль."""
    return call("POST", "/purchase-orders/", json={
        "external_id": external_id,           # номер замовлення в CRM (унікальний)
        "supplier": {"name": "Acme Components GmbH", "email": "sales@acme.example",
                     "country": "DE", "currency": "EUR"},
        "supplier_order_no": "AB-7781",       # номер підтвердження постачальника
        "order_date": date.today().isoformat(),
        "expected_date": (date.today() + timedelta(days=10)).isoformat(),
        "currency": "EUR",
        "lines": lines,
        **extra,
    }, quiet=quiet)


def send_tracking(external_id, tracking, carrier="", expected="", status="in_transit", quiet=False):
    body = {"tracking_number": tracking, "carrier": carrier, "status": status}
    if expected:
        body["expected_date"] = expected  # стане орієнтовною датою замовлення
    if "/" in external_id:  # номер зі «/» не вміщується в адресу — передаємо в тілі
        return call("POST", "/purchase-orders/shipments/", json={"external_id": external_id, **body}, quiet=quiet)
    return call("POST", ref(external_id) + "shipments/", json=body, quiet=quiet)


def send_delivered(external_id, delivery_id="", lines=None, tracking="", quiet=False):
    """Постачальник / перевізник повідомив «доставлено». Повтор — duplicate, склад не змінюється."""
    body = {"delivered_at": datetime.now(timezone.utc).isoformat()}
    if delivery_id:
        body["delivery_id"] = delivery_id     # напр. номер накладної (Lieferschein)
    if tracking:
        body["tracking_number"] = tracking
    if lines:
        body["lines"] = lines                 # частина: лише ці позиції
    if "/" in external_id:
        return call("POST", "/purchase-orders/deliveries/", json={"external_id": external_id, **body}, quiet=quiet)
    return call("POST", ref(external_id) + "deliveries/", json=body, quiet=quiet)


def n(value):
    return f"{float(value or 0):g}"


def summary(po):
    lines = ", ".join(f"{ln['sku']} {n(ln['qty_received'])}/{n(ln['qty_ordered'])}" for ln in po.get("lines", []))
    return (f"{po['code']} ({po['external_id']}) — {po['status']}, отримано {n(po['qty_received'])} "
            f"з {n(po['qty_ordered'])} · {lines} · очікується {po.get('expected_date') or '—'}")


def stock_line(sku):
    st, s = call("GET", "/stock/" + quote(sku, safe="") + "/", quiet=True)
    if st == 403:
        return f"{sku}: склад не показано — у ключа немає права stock:read (для демо додайте його)"
    if not isinstance(s, dict) or "on_hand" not in s:
        return f"{sku}: немає в Minerva"
    return f"{sku}: на складі {n(s['on_hand'])}, очікується {n(s.get('incoming'))}"


def demo(sku):
    """Увесь цикл на одному артикулі: замовили 10 → трек → доставлено 4 → повтор → решта."""
    number = f"DEMO-{int(time.time())}"
    print(f"Демо-закупівля {number}, артикул {sku}. Склад до:\n  {stock_line(sku)}\n")

    st, po = send_order(number, [{"line_id": "1", "sku": sku, "qty": 10, "unit_price": "12.50"}], quiet=True)
    print(f"1. Замовлення передано → {st}, created={po.get('created')}, warnings={po.get('warnings')}\n  {summary(po)}")
    print(f"  {stock_line(sku)}  ← «очікується» виросло")

    st, po = send_order(number, [{"line_id": "1", "sku": sku, "qty": 10, "unit_price": "12.50"}], quiet=True)
    print(f"2. Той самий запит ще раз (повтор після тайм-ауту) → {st}, created={po.get('created')} — дубля немає")

    st, po = send_tracking(number, f"1Z{int(time.time())}", "UPS",
                           (date.today() + timedelta(days=5)).isoformat(), quiet=True)
    print(f"3. Трек-номер → {st}\n  {summary(po)}")

    st, po = send_delivered(number, "LS-1", [{"line_id": "1", "qty": 4}], quiet=True)
    d = po.get("delivery", {})
    print(f"4. Доставлено 4 шт (накладна LS-1) → {st}, duplicate={d.get('duplicate')}, booked={d.get('booked')}"
          f"\n  {summary(po)}\n  {stock_line(sku)}")

    st, po = send_delivered(number, "LS-1", [{"line_id": "1", "qty": 4}], quiet=True)
    print(f"5. Те саме «доставлено» ще раз → {st}, duplicate={po.get('delivery', {}).get('duplicate')}"
          f" — на склад нічого не додано\n  {stock_line(sku)}")

    st, po = send_delivered(number, "LS-2", [{"line_id": "1", "qty": 9}], quiet=True)
    print(f"6. Нова накладна LS-2 на 9 шт (лишилось лише 6) → {st} {po.get('code')}: {po.get('detail', '')}")

    st, po = send_delivered(number, "LS-2", quiet=True)
    print(f"7. LS-2 без позицій = уся решта → {st}\n  {summary(po)}\n  {stock_line(sku)}")
    print(f"\nГотово. Подивитись: GET {ref(number)}  ·  в адмінці: Склад → Закупівлі, колонка «Звідки»")


def main(cmd="help", *args):
    if cmd == "ping":
        call("GET", "/ping/")

    elif cmd == "order":
        # Замовлення постачальнику з CRM (повтор того самого номера — оновлення)
        send_order(args[0], parse_lines(args[1:]) or [{"line_id": "1", "sku": "AMP-100", "qty": 10}])

    elif cmd == "get":
        if "/" in args[0]:
            call("GET", "/purchase-orders/", params={"external_id": args[0]})
        else:
            call("GET", ref(args[0]))

    elif cmd == "list":
        call("GET", "/purchase-orders/", params={"open": 1, "page_size": 50})

    elif cmd == "track":
        # track <номер> <трек-номер> [перевізник] [орієнтовна дата YYYY-MM-DD]
        send_tracking(args[0], args[1], *(args[2:4]))

    elif cmd == "delivered":
        # delivered <номер> [delivery_id] [SKU:qty ...] — без позицій = уся решта
        rest = list(args[1:])
        delivery_id = rest.pop(0) if rest and ":" not in rest[0] else ""
        lines = [{"sku": ln["sku"], "qty": ln["qty"]} for ln in parse_lines(rest)]
        send_delivered(args[0], delivery_id, lines)

    elif cmd == "cancel":
        reason = args[1] if len(args) > 1 else "Скасовано в CRM"
        if "/" in args[0]:
            sys.exit("Номер зі «/»: скасування — PATCH з тілом {\"status\": \"cancelled\"} за кодом PO-…")
        call("POST", ref(args[0]) + "cancel/", json={"reason": reason})

    elif cmd == "sync":
        # Страховка до вебхука purchase_order.updated: забираємо лише змінені з минулого запуску
        since = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else None
        started = datetime.now(timezone.utc).isoformat()
        params = {"changed_since": since, "page_size": 100} if since else {"page_size": 100}
        url, count = "/purchase-orders/", 0
        while url:
            st, data = call("GET", url, quiet=True, params=params)
            if st != 200:
                sys.exit(f"GET /purchase-orders/ → {st}: {data}")
            for po in data["results"]:
                count += 1
                print(summary(po))   # тут — оновлення стану замовлення у вашій CRM
            url, params = data.get("next"), {}
        with open(STATE_FILE, "w") as f:
            f.write(started)
        print(f"Змінених закупівель: {count} (з {since or 'початку'})")

    elif cmd == "demo":
        skus = [a for a in args if a != "--yes"]
        sku = skus[0] if skus else "AMP-100"
        if "--yes" not in args:
            answer = input(f"Демо створить справжню закупівлю і оприбуткує 10 шт «{sku}» на склад. Далі? [y/N] ")
            if answer.strip().lower() not in ("y", "yes", "т", "так"):
                return
        demo(sku)

    else:
        print(__doc__)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Задайте MINERVA_TOKEN (ключ з правами purchases:read + purchases:write).")
    main(*sys.argv[1:])
