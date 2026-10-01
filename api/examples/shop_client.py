"""
Приклад інтеграції інтернет-магазину з Minerva API.

Запуск (потрібен лише пакет requests):
    set MINERVA_URL=http://localhost:8000          (PowerShell: $env:MINERVA_URL="...")
    set MINERVA_TOKEN=<ключ з Адмін → REST API → API Ключі>
    python api/examples/shop_client.py ping
    python api/examples/shop_client.py stock
    python api/examples/shop_client.py stock AMP-100
    python api/examples/shop_client.py check AMP-100:2 CABLE-M:5
    python api/examples/shop_client.py receive AMP-100 10
    python api/examples/shop_client.py order AMP-100:1
    python api/examples/shop_client.py track WS-1001
    python api/examples/shop_client.py sync            # інкрементальна синхронізація залишків

Ключ НЕ можна вставляти в JavaScript на сторінці магазину — тільки у серверний код.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# Windows-консоль (cp1251) не друкує ✓/→ — перемикаємо вивід на UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE  = os.getenv("MINERVA_URL", "http://localhost:8000").rstrip("/") + "/api/v1"
TOKEN = os.getenv("MINERVA_TOKEN", "")

session = requests.Session()
session.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})


def call(method, path, **kw):
    r = session.request(method, BASE + path, timeout=20, **kw)
    if r.status_code == 429:  # ліміт запитів — чекаємо і повторюємо
        time.sleep(int(r.headers.get("Retry-After", 5)))
        return call(method, path, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.text
    print(f"{method} {path} → {r.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2, default=str))
    return r.status_code, body


def iter_pages(path, params=None):
    """Обхід усіх сторінок списку (?page=N)."""
    url = BASE + path
    while url:
        r = session.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        yield from data["results"]
        url, params = data.get("next"), None


def parse_items(args):
    return [{"sku": a.rsplit(":", 1)[0], "qty": float(a.rsplit(":", 1)[1]) if ":" in a else 1}
            for a in args]


def main(cmd, *args):
    if cmd == "ping":
        call("GET", "/ping/")

    elif cmd == "stock":
        call("GET", f"/stock/{args[0]}/" if args else "/stock/?page_size=50")

    elif cmd == "check":
        # Перед оформленням кошика: чи все є в наявності?
        call("POST", "/stock/check/", json={"items": parse_items(args)})

    elif cmd == "receive":
        # Прихід товару; external_key захищає від дублювання при повторі запиту
        sku, qty = args[0], args[1]
        call("POST", "/stock-movements/", json={
            "sku": sku, "tx_type": "Incoming", "qty": qty,
            "ref_doc": "Прихід з прикладу", "external_key": f"example-{sku}-{int(time.time())}",
        })

    elif cmd == "order":
        # Те, що робить магазин після оплати замовлення (webhook / cron)
        number = f"WS-{int(time.time())}"
        call("POST", "/orders/", json={
            "order_number": number,           # номер замовлення в магазині (унікальний)
            "source": "webshop",              # або задати 'Джерело замовлень' у ключі
            "order_date": datetime.now().date().isoformat(),
            "currency": "EUR",
            "client": "Max Mustermann",
            "email": "max@example.com",
            "phone": "+49 30 123456",
            "addr_street": "Hauptstr. 1", "addr_city": "Berlin",
            "addr_zip": "10115", "addr_country": "DE",
            "shipping_cost": 4.90,
            "check_stock": True,              # 409, якщо товару не вистачає
            "note": "Створено з прикладу shop_client.py",
            "lines": parse_items(args) or [{"sku": "AMP-100", "qty": 1}],
        })
        print(f"\nНомер замовлення: {number}")

    elif cmd == "cancel":
        call("POST", f"/orders/{args[0]}/cancel/", json={"restock": True})

    elif cmd == "track":
        # Статус замовлення + трекінг для сторінки «Мої замовлення»
        _, orders = call("GET", "/orders/", params={"order_number": args[0]})
        if orders.get("results"):
            call("GET", f"/orders/{orders['results'][0]['id']}/")
        call("GET", "/shipments/", params={"order_number": args[0]})

    elif cmd == "sync":
        # Інкрементальна синхронізація: тягнемо лише товари, що змінились з минулого запуску
        state_file = os.path.join(os.path.dirname(__file__), ".last_sync")
        since = open(state_file).read().strip() if os.path.exists(state_file) else None
        started = datetime.now(timezone.utc).isoformat()
        params = {"changed_since": since} if since else {}
        n = 0
        for item in iter_pages("/stock/", params):
            n += 1
            # тут — оновлення наявності у вашому магазині
            print(f"{item['sku']:<25} available={item['available']}")
        with open(state_file, "w") as f:
            f.write(started)
        print(f"Оновлено товарів: {n} (з {since or 'початку'})")

    else:
        print(__doc__)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Задайте MINERVA_TOKEN (ключ з Адмін → REST API → API Ключі).")
    main(*(sys.argv[1:] or ["help"]))
