"""
Розподільник app.sevskiy.com на RAG-ПК (Minerva, 22.10.2026). Лише стандартна бібліотека Python.
На RAG-ПК лежить як D:\\RAG-router\\router.py (задача «Minerva-RAG-Router»); перевірка — check_rag_router.py.

DSM-проксі 192.168.2.142 → portproxy 192.168.2.68:8787 → цей розподільник 127.0.0.1:8786:
  /v2/…  → приватний RAG 127.0.0.1:8789 — лише з публічної адреси Minerva (DNS allow_hosts), інакше 403;
  решта  → публічний RAG 127.0.0.1:8787 без змін (код публічного RAG не чіпаємо).

Адресу клієнта DSM передає в X-Real-IP / X-Forwarded-For (останній елемент — той, кого бачив DSM).
Перевіряються всі заголовки з trusted_headers: кожен має бути і вказувати на дозволену адресу.
Тіла запитів, query і Authorization не журналюються. Налаштування — router.json поруч.
"""
import http.client
import json
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "router.log"
DEFAULTS = {
    "listen_port": 8786,
    "public_port": 8787,
    "private_port": 8789,
    "private_prefix": "/v2",
    "allow_hosts": ["sevskiy.minerva-bi.com"],
    "allow_ips": [],
    "trusted_headers": ["x-real-ip", "x-forwarded-for"],
    "resolve_seconds": 300,
    "timeout_seconds": 120,
}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade"}
SKIP_RESPONSE = HOP | {"content-length", "server", "date"}
_log_lock = threading.Lock()


def log(text):
    with _log_lock:
        LOG.parent.mkdir(exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > 5 * 1024 * 1024:
            old = LOG.with_name(LOG.name + ".1")
            old.unlink(missing_ok=True)
            LOG.rename(old)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")


def load_config():
    config = dict(DEFAULTS)
    path = ROOT / "router.json"
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8-sig")))
    return config


class Allowlist:
    """Дозволені адреси для /v2: allow_ips + DNS allow_hosts (оновлюється; збій DNS лишає попередні)."""

    def __init__(self, config):
        self.config = config
        self.ips = set(config["allow_ips"])
        self.resolved_at = 0.0
        self.lock = threading.Lock()

    def current(self):
        with self.lock:
            if time.monotonic() - self.resolved_at >= self.config["resolve_seconds"]:
                resolved = set()
                for host in self.config["allow_hosts"]:
                    try:
                        resolved |= {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
                    except OSError as exc:
                        log(f"DNS {host}: {type(exc).__name__} — лишаю попередні адреси")
                        resolved = None
                        break
                if resolved is not None:
                    fresh = set(self.config["allow_ips"]) | resolved
                    if fresh != self.ips:
                        log("дозволені адреси /v2: " + ", ".join(sorted(fresh)))
                    self.ips = fresh
                self.resolved_at = time.monotonic()
            return self.ips


def client_addresses(headers, trusted):
    """Адреси клієнта з довірених заголовків DSM; None — якщо якогось заголовка немає."""
    found = []
    for name in trusted:
        value = (headers.get(name) or "").strip()
        if not value:
            return None
        found.append(value.split(",")[-1].strip() if name == "x-forwarded-for" else value)
    return found


def error_body(code, message, retryable):
    """Формат помилок приватного API (error.schema.json контракту rag-private/2.0)."""
    return {"code": code, "message": message, "retryable": retryable, "request_id": uuid.uuid4().hex}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rag-router"
    config = None
    allowlist = None

    def log_message(self, fmt, *args):   # без стандартного журналу в stderr (pythonw)
        pass

    def _send(self, status, payload, reason=None, headers=()):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status, reason)
        for key, value in headers:
            self.send_header(key, value)
        if not any(k.lower() == "content-type" for k, _ in headers):
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _proxy(self):
        started = time.monotonic()
        cfg = self.config
        path = self.path.split("?", 1)[0]
        prefix = cfg["private_prefix"]
        private = path == prefix or path.startswith(prefix + "/")
        if private:
            seen = client_addresses(self.headers, cfg["trusted_headers"])
            allowed = self.allowlist.current()
            if seen is None or not all(ip in allowed for ip in seen):
                log(f"/v2 ВІДМОВА {self.command} {path} real-ip={self.headers.get('X-Real-IP', '-')} "
                    f"xff={self.headers.get('X-Forwarded-For', '-')}")
                return self._send(403, error_body("unauthorized", "Адреса клієнта не дозволена для приватного API.",
                                                  False))
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._send(411, {"error": "Content-Length required"})
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        port = cfg["private_port"] if private else cfg["public_port"]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=cfg["timeout_seconds"])
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status, reason, resp_headers = resp.status, resp.reason, resp.getheaders()
        except (OSError, http.client.HTTPException) as exc:
            log(f"{'приватний' if private else 'публічний'} RAG недоступний: {type(exc).__name__} {path}")
            if private:
                return self._send(503, error_body("unavailable", "Приватний RAG тимчасово недоступний.", True))
            return self._send(502, {"error": "RAG API недоступний"})
        finally:
            conn.close()
        if private:
            log(f"/v2 {self.command} {path} → {status} {int((time.monotonic() - started) * 1000)} мс "
                f"real-ip={self.headers.get('X-Real-IP', '-')}")
        self._send(status, data, reason, [(k, v) for k, v in resp_headers if k.lower() not in SKIP_RESPONSE])

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _proxy


def main():
    config = load_config()
    Handler.config = config
    Handler.allowlist = Allowlist(config)
    Handler.allowlist.current()
    while True:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", int(config["listen_port"])), Handler)
            server.daemon_threads = True
            log(f"розподільник слухає 127.0.0.1:{config['listen_port']} (публічний → {config['public_port']}, "
                f"{config['private_prefix']} → {config['private_port']})")
            server.serve_forever()
        except OSError as exc:
            log(f"не вдалося слухати порт {config['listen_port']}: {exc} — повтор через 15 с")
            time.sleep(15)
        except Exception:  # noqa: BLE001
            log("збій розподільника:\n" + traceback.format_exc())
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
