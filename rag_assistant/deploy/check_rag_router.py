"""Перевірка rag_router.py без RAG-ПК: підставні публічний і приватний RAG, розподільник у тимчасовій теці.
    python rag_assistant/deploy/check_rag_router.py"""
import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent


def backend(name):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            status = 401 if self.path.startswith("/v2/capabilities") else 200
            data = json.dumps({"backend": name, "path": self.path, "body": body.decode(),
                               "auth": bool(self.headers.get("Authorization"))}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Test", name)
            self.send_header("Set-Cookie", "a=1")
            self.send_header("Set-Cookie", "b=2")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = _reply
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def call(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), r.getheader("Set-Cookie"), r.msg.get_all("Set-Cookie"), data


def main():
    public, private = backend("public"), backend("private")
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(HERE / "rag_router.py", tmp / "router.py")
    port = 18786
    (tmp / "router.json").write_text(json.dumps({
        "listen_port": port, "public_port": public.server_port, "private_port": private.server_port,
        "allow_hosts": [], "allow_ips": ["203.0.113.5"]}), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(tmp / "router.py")])
    ok = True
    try:
        for _ in range(50):
            try:
                call(port, "GET", "/v1/health")
                break
            except OSError:
                time.sleep(0.1)

        def check(label, cond):
            nonlocal ok
            ok &= bool(cond)
            print(("OK  " if cond else "FAIL") + " " + label)

        s, h, _, cookies, d = call(port, "GET", "/v1/health?x=1", {"Authorization": "Bearer secret-public"})
        j = json.loads(d)
        check("public GET passes through", s == 200 and j["backend"] == "public" and j["path"] == "/v1/health?x=1"
              and j["auth"] and h.get("X-Test") == "public")
        check("multiple Set-Cookie kept", cookies == ["a=1", "b=2"])
        s, _, _, _, d = call(port, "POST", "/v1/ask", {"Content-Type": "application/json"}, b'{"q":"\xd0\x90"}')
        check("public POST body passes", s == 200 and json.loads(d)["body"] == '{"q":"А"}')
        s, _, _, _, d = call(port, "GET", "/v2/capabilities")
        e = json.loads(d)
        check("/v2 without headers -> 403 contract error",
              s == 403 and e["code"] == "unauthorized" and 8 <= len(e["request_id"]) <= 64 and e["retryable"] is False)
        good = {"X-Real-IP": "203.0.113.5", "X-Forwarded-For": "203.0.113.5", "Authorization": "Bearer k"}
        s, _, _, _, d = call(port, "GET", "/v2/capabilities?source_instance_id=x", good)
        check("/v2 from allowed IP -> private backend", s == 401 and json.loads(d)["backend"] == "private")
        s, _, _, _, _ = call(port, "GET", "/v2", good)
        check("/v2 exact path -> private", s == 200)
        s, _, _, _, _ = call(port, "GET", "/v2x/test", good)
        check("/v2x is not private", s == 200)
        spoof = dict(good, **{"X-Forwarded-For": "203.0.113.5, 198.51.100.9"})
        s, _, _, _, _ = call(port, "GET", "/v2/capabilities", spoof)
        check("forged XFF with real last hop -> 403", s == 403)
        s, _, _, _, _ = call(port, "GET", "/v2/capabilities", {"X-Real-IP": "203.0.113.5"})
        check("only one trusted header -> 403", s == 403)
        s, _, _, _, _ = call(port, "GET", "/v2/capabilities", {"X-Real-IP": "198.51.100.9", "X-Forwarded-For": "203.0.113.5"})
        check("X-Real-IP not allowed -> 403", s == 403)
        private.shutdown()
        private.server_close()
        s, _, _, _, d = call(port, "GET", "/v2/capabilities", good)
        check("private down -> 503 unavailable", s == 503 and json.loads(d)["code"] == "unavailable")
        public.shutdown()
        public.server_close()
        s, _, _, _, _ = call(port, "GET", "/v1/health")
        check("public down -> 502", s == 502)
        log = (tmp / "logs" / "router.log").read_text(encoding="utf-8")
        check("log has no Authorization / query", "secret" not in log and "Bearer" not in log
              and "source_instance_id" not in log)
        print("---- router.log\n" + log)
    finally:
        proc.terminate()
        proc.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL OK" if ok else "SOME FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
