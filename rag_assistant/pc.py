"""Панель «🖥 RAG-ПК зараз» на сторінці «📚 Помічник по продукції»: чим відповідає RAG, що він робить, зупинка.

Лише через RAG API v1 тим самим ключем (адреса й ключ — RagSettings): /v1/health (модель, без ключів),
/v1/metrics (запити за клієнтами), /v1/runtime (відеопам'ять, завантажені моделі); дії — /v1/runs/{id}/cancel
(як «Зупинити запит» у GUI на ПК) і /v1/runtime/unload. Старіший RAG API без цих викликів — зрозуміла підказка.
"""
from __future__ import annotations

import time

from .client import RagClient, RagError
from .models import RagSettings

PANEL_TIMEOUT = 8
RECENT = 2 * 3600        # клієнти, що зверталися за останні 2 год
UPDATE_HINT = "оновіть rag_api.py на RAG-ПК і перезапустіть задачу «RAG-Integration-API»"


def client_label(client: str) -> str:
    """Хто звертається до RAG — зрозумілою мовою."""
    c = client or ""
    if c == "api:shop-search":
        return "Підказки в пошуку сайту (Minerva)"
    if c.startswith("api:site:"):
        return "Чат «Frag Minerva» на сайті"
    if c.startswith("api:minerva-test:"):
        return "Перевірка RAG з картки магазину"
    if c.startswith("api:minerva:admin"):
        return "Перевірка зв'язку з Minerva"
    if c.startswith("api:minerva"):
        return "Помічник по продукції в Minerva"
    if c.startswith("api:web:"):
        return "Вебчат RAG"
    if c == "desktop":
        return "GUI на RAG-ПК"
    return c.removeprefix("api:") or "?"


def _client(settings=None) -> RagClient:
    client = RagClient(settings or RagSettings.get(), client_id="minerva:pc-panel")
    client.timeout = PANEL_TIMEOUT
    return client


def status() -> dict:
    """Стан для панелі (опитується кожні 5 с, поки сторінка відкрита)."""
    try:
        client = _client()
    except RagError as e:
        return {"ok": False, "error": e.message}
    out = {"ok": True}
    try:
        health = client.health()
    except RagError as e:
        return {"ok": False, "error": f"RAG-ПК не відповів: {e.message}"}
    llm = health.get("llm") if isinstance(health.get("llm"), dict) else None
    out["llm"] = llm
    if llm is None:
        out["llm_note"] = "RAG ще не повідомляє модель — " + UPDATE_HINT
    try:
        rt = client.runtime()
        gpu = rt.get("gpu") or {}
        out["runtime"] = {"vram_used_mb": gpu.get("vram_used_mb"), "vram_total_mb": gpu.get("vram_total_mb"),
                          "models": [m.get("name") for m in rt.get("models") or [] if isinstance(m, dict)],
                          "ollama": bool(rt.get("ollama_available", True))}
    except RagError:
        out["runtime"] = None
    try:
        m = client.metrics()
    except RagError as e:
        out["metrics_error"] = e.message
        return out
    now = float(m.get("time") or time.time())
    out["active"] = [{"id": r.get("id"), "client": client_label(r.get("client")), "stage": r.get("stage") or "",
                      "seconds": int(now - float(r.get("started") or now)), "stopping": bool(r.get("cancel_requested"))}
                     for r in m.get("runs") or [] if r.get("status") == "running"]
    out["recent"] = [{"client": client_label(r.get("client")), "status": r.get("status"), "stage": r.get("stage") or "",
                      "seconds": round(float(r.get("finished") or now) - float(r.get("started") or now), 1),
                      "ago": int(now - float(r.get("started") or now))}
                     for r in (m.get("runs") or [])[:8] if r.get("status") != "running"]
    out["clients"] = [{"client": client_label(c.get("id")), "requests": c.get("requests") or 0,
                       "completed": c.get("completed") or 0, "errors": c.get("errors") or 0, "busy": c.get("busy") or 0,
                       "avg": round(c["avg_seconds"], 1) if c.get("avg_seconds") else None}
                      for c in m.get("clients") or [] if now - float(c.get("last_seen") or 0) < RECENT]
    return out


def cancel(run_id: str) -> dict:
    try:
        data = _client().cancel_run(run_id)
    except RagError as e:
        if e.code == "not_found":
            return {"ok": False, "text": "Запит уже завершився або RAG ще не вміє зупиняти через API — " + UPDATE_HINT}
        return {"ok": False, "text": e.message}
    return {"ok": True, "text": str(data.get("message") or "Зупинку запитано.")}


def unload() -> dict:
    try:
        data = _client().unload()
    except RagError as e:
        text = "RAG зараз відповідає на запит — спробуйте після нього" if e.status == 409 else e.message
        return {"ok": False, "text": text}
    return {"ok": True, "text": str(data.get("message") or "Моделі вивантажено.")}
