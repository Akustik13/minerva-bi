"""
Тестовий приймач вебхуків Minerva — показує події і перевіряє підпис.

    set WEBHOOK_SECRET=whsec_...        (PowerShell: $env:WEBHOOK_SECRET="whsec_...")
    python api/examples/webhook_receiver.py            # слухає http://0.0.0.0:9000/

Далі в Minerva: Адмін → REST API → Вебхуки → URL http://<IP цього ПК>:9000/
→ дія «Надіслати тестову подію (ping)».
Лише стандартна бібліотека Python.
"""
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# Windows-консоль (cp1251) не друкує ✓/→ — перемикаємо вивід на UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "9000"))
seen = set()  # X-Minerva-Delivery — для відсіву повторів


def verify(secret, body, header, tolerance=300):
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, sig = int(parts["t"]), parts["v1"]
    except (ValueError, KeyError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        event = self.headers.get("X-Minerva-Event")
        delivery = self.headers.get("X-Minerva-Delivery")
        signature = self.headers.get("X-Minerva-Signature", "")

        if SECRET and not verify(SECRET, body, signature):
            print(f"✗ {event}: невірний підпис")
            self.send_response(401)
            self.end_headers()
            return

        if delivery in seen:
            print(f"↺ {event}: повтор {delivery} — пропущено")
        else:
            seen.add(delivery)
            payload = json.loads(body)
            print(f"✓ {event} [{delivery}]")
            print(json.dumps(payload["data"], ensure_ascii=False, indent=2))

        # Відповідайте 2xx швидко (<10 с); важку обробку — у фон/чергу
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Слухаю http://0.0.0.0:{PORT}/  (перевірка підпису: {'так' if SECRET else 'НІ — задайте WEBHOOK_SECRET'})")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
