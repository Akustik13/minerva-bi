"""python -m bench [--root D:\\Meeting] [--port 8791] [--no-tls]"""
import argparse
import logging
import logging.handlers
import os
import ssl
import sys
from pathlib import Path

from aiohttp import web

from . import VERSION, certs, config
from .server import create_app


def main():
    default_root = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(prog="bench")
    p.add_argument("--root", default=str(default_root))
    p.add_argument("--port", type=int)
    p.add_argument("--host")
    p.add_argument("--no-tls", action="store_true", help="plain HTTP (only behind a TLS proxy or for tests)")
    args = p.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    cfg = config.load(args.root)
    if args.port:
        cfg.override("port", args.port)
    if args.host:
        cfg.override("host", args.host)

    handlers = [logging.handlers.RotatingFileHandler(cfg.logs_dir / "bench.log", maxBytes=5 * 2**20, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    log = logging.getLogger("bench")
    if cfg.error:
        log.warning("%s", cfg.error)

    ssl_ctx = None
    if cfg["tls"] and not args.no_tls:
        cert, key, fingerprint = certs.ensure(cfg)
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(str(cert), str(key))
        log.info("TLS certificate SHA-256 %s", fingerprint)
    scheme = "https" if ssl_ctx else "http"
    log.info("meeting bench %s on %s://%s:%s (root %s)", VERSION, scheme, cfg["host"], cfg["port"], cfg.root)
    web.run_app(create_app(cfg), host=cfg["host"], port=int(cfg["port"]), ssl_context=ssl_ctx,
                access_log=None, print=None, handle_signals=sys.platform != "win32")


if __name__ == "__main__":
    main()
