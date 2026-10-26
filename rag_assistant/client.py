"""Клієнт RAG API v1 (сервер → сервер, Bearer-ключ). Документація RAG: API_INTEGRATION.md.

POST /v1/ask → 202 {job_id}; GET /v1/jobs/{id} → {status: running|completed|failed, stage, trace, result}.
RAG виконує один запит одночасно: 429 = зайнято (запит не прийнято, можна повторити пізніше).
"""
from __future__ import annotations

import re

import requests


class RagError(Exception):
    """Помилка RAG з кодом для інтерфейсу: unconfigured, busy, auth, bad_request, not_found, unavailable, network."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


_MESSAGES = {
    400: ("bad_request", "Некоректний запит до помічника."),
    401: ("auth", "Помічник відхилив ключ API — перевірте налаштування."),
    404: ("not_found", "Завдання не знайдено (минула година або RAG перезапускали)."),
    413: ("bad_request", "Запит завеликий."),
    429: ("busy", "Помічник зараз обробляє інший запит."),
    503: ("unavailable", "Сервіс помічника тимчасово недоступний."),
}


class RagClient:
    def __init__(self, settings, client_id: str = "minerva"):
        if not settings.configured:
            raise RagError("unconfigured", "Помічник не налаштований (адреса або ключ API).")
        self.base = settings.base_url.rstrip("/")
        self.timeout = max(3, int(settings.timeout or 20))
        self.verify = bool(settings.verify_tls)
        cid = re.sub(r"[^A-Za-z0-9_.:-]", "-", client_id)[:80] or "minerva"
        self.headers = {"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json",
                        "X-Client-ID": cid}

    def _call(self, method: str, path: str, body=None) -> dict:
        try:
            r = requests.request(method, self.base + path, json=body, headers=self.headers,
                                 timeout=self.timeout, verify=self.verify)
        except requests.exceptions.SSLError:
            raise RagError("network", "Помилка HTTPS-сертифіката адреси помічника.")
        except requests.exceptions.Timeout:
            raise RagError("network", "Помічник не відповів вчасно.")
        except requests.exceptions.RequestException:
            raise RagError("network", "Немає зв'язку з помічником.")
        if r.status_code in _MESSAGES:
            code, msg = _MESSAGES[r.status_code]
            raise RagError(code, msg, r.status_code)
        if r.status_code >= 400:
            raise RagError("unavailable", f"Помічник повернув помилку HTTP {r.status_code}.", r.status_code)
        try:
            return r.json()
        except ValueError:
            raise RagError("unavailable", "Відповідь помічника не JSON — перевірте адресу (можливо, це не RAG API).")

    # ── API ──
    def health(self) -> dict:
        return self._call("GET", "/v1/health")

    def documents(self) -> list:
        return self._call("GET", "/v1/documents").get("documents", [])

    def ask(self, question: str, history: list, language: str = "uk", latest: bool = True,
            document_id: str | None = None, path: str = "/v1/ask") -> str:
        body = {"question": question[:3000], "history": history[-8:], "language": language, "latest": latest}
        if document_id:
            body["document_id"] = document_id
        data = self._call("POST", path, body)
        if not data.get("job_id"):
            raise RagError("unavailable", "Помічник не повернув номер завдання.")
        return data["job_id"]

    def search(self, question: str, history: list, language: str = "uk", latest: bool = True) -> str:
        """Лише пошук, без моделі (/v1/search): даташити й каталог → products, sources; швидше й без оплати LLM."""
        return self.ask(question, history, language=language, latest=latest, path="/v1/search")

    def job(self, job_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or ""):
            raise RagError("not_found", "Некоректний номер завдання.")
        return self._call("GET", f"/v1/jobs/{job_id}")

    # ── Стан і керування RAG-ПК (панель «🖥 RAG-ПК зараз») ──
    def metrics(self) -> dict:
        return self._call("GET", "/v1/metrics")

    def runtime(self) -> dict:
        return self._call("GET", "/v1/runtime")

    def cancel_job(self, job_id: str) -> dict:
        """Зупинити своє завдання (RAG API з /v1/jobs/{id}/cancel; старіший → RagError not_found)."""
        if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
            raise RagError("not_found", "Некоректний номер завдання.")
        return self._call("POST", f"/v1/jobs/{job_id}/cancel", {})

    def cancel_run(self, run_id: str) -> dict:
        """Зупинити будь-який запит зі списку /v1/metrics (як «Зупинити запит» у GUI RAG-ПК)."""
        if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
            raise RagError("not_found", "Некоректний номер запиту.")
        return self._call("POST", f"/v1/runs/{run_id}/cancel", {})

    def unload(self) -> dict:
        """Вивантажити моделі Ollama з відеопам'яті (наступний запит завантажить потрібну сам)."""
        return self._call("POST", "/v1/runtime/unload", {})
