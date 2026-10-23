"""Прийняття production sevskiy-prod: лише читання й пошуки, нічого не змінює. Виводить так/ні й лічильники —
без тем, адрес і текстів листів. Запуск на RAG-ПК: D:\\RAG-private-prod\\venv\\Scripts\\python.exe -B prod_acceptance.py"""
import json
import random
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DATA = Path(r"D:\RAG-private-prod\data")
cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8-sig"))
KEY = (DATA / "private-api-key.txt").read_text(encoding="utf-8-sig").strip()
QKEY = (DATA / "qdrant-api-key.txt").read_text(encoding="utf-8-sig").strip()
SRC, TENANT = cfg["source"]["source_instance_id"], cfg["source"]["tenant"]
BASE = f"http://127.0.0.1:{cfg['port']}"
QDRANT = cfg["backend"]["qdrant_url"].rstrip("/") + "/collections/" + cfg["backend"]["collection"]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
results = []


def call(url, body=None, headers=None, method=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


AUTH = {"Authorization": "Bearer " + KEY}
db = sqlite3.connect(f"file:{(DATA / 'state.sqlite3').as_posix()}?mode=ro", uri=True)
state = json.loads(db.execute("SELECT v FROM kv WHERE kind='source' AND k=?", (f"{TENANT}:{SRC}",)).fetchone()[0])
GEN = state["generation"]
prefix = f"{TENANT}:{SRC}:{GEN}:"
docs = {k[len(prefix):]: json.loads(v) for k, v in db.execute("SELECT k, v FROM kv WHERE kind='docs'") if k.startswith(prefix)}
active = {k: d for k, d in docs.items() if d.get("active")}


def search(query, user, mailboxes, limit=20, generation=GEN, extra=None):
    body = {"source_instance_id": SRC, "generation": generation, "query": query[:3000],
            "filters": {"user_id": user, "mailbox_ids": mailboxes, **(extra or {})}, "limit": limit, "wait_ms": 8000}
    status, value = call(BASE + "/v2/retrieval/jobs", body, {**AUTH, "Idempotency-Key": "acceptance:" + uuid.uuid4().hex})
    deadline = time.monotonic() + 30
    while status == 202 and time.monotonic() < deadline:
        time.sleep(0.5)
        status, job = call(BASE + f"/v2/jobs/{value['job_id']}?source_instance_id={SRC}&generation={GEN}&user_id={user}", headers=AUTH)
        if job.get("status") in ("completed", "failed", "cancelled"):
            return (200 if job["status"] == "completed" else 503), job.get("result", job), value["job_id"]
        status = 202
    return status, value, value.get("job_id")


# 1. Стан і узгодженість з Minerva
st, caps = call(BASE + f"/v2/capabilities?source_instance_id={SRC}&generation={GEN}&user_id=1", headers=AUTH)
check("capabilities: retrieval ready", st == 200 and caps["readiness"]["retrieval"] == "ready", f"indexing={caps.get('readiness', {}).get('indexing')}")
m = state.get("minerva_documents") or {}
check("індекс = Minerva (eligible = indexed = активні в RAG)", m.get("eligible") == m.get("indexed") == len(active),
      f"Minerva {m.get('eligible')}/{m.get('indexed')}, RAG {len(active)}, pending {m.get('pending')}+{m.get('pending_delete')}")
check("журнал застосовано повністю", state.get("applied_seq") == state.get("received_seq") == state.get("last_seq"),
      f"seq {state.get('applied_seq')}")

# 2. Без дублікатів: у Qdrant кожен лист лише в одній (чинній) ревізії, старих поколінь немає
revs, other_gen, points, offset = {}, 0, 0, None
while True:
    body = {"limit": 2000, "with_payload": ["document_id", "document_revision", "generation", "source_instance_id"],
            "with_vector": False, "filter": {"must": [{"key": "tenant", "match": {"value": TENANT}}]}}
    if offset is not None:
        body["offset"] = offset
    _, page = call(QDRANT + "/points/scroll", body, {"api-key": QKEY}, timeout=60)
    for p in page["result"]["points"]:
        points += 1
        pl = p["payload"]
        if pl.get("generation") != GEN:
            other_gen += 1
            continue
        revs.setdefault(pl["document_id"], set()).add(pl["document_revision"])
    offset = page["result"].get("next_page_offset")
    if offset is None:
        break
multi = sum(1 for v in revs.values() if len(v) > 1)
stale = sum(1 for d, v in revs.items() if d in active and v != {active[d]["revision"]})
with_text = {d for d, x in active.items() if x.get("chunks")}       # лист без тексту й вкладень не має точок
check("Qdrant: жодного листа у двох ревізіях", multi == 0, f"точок {points}, листів {len(revs)}")
check("Qdrant: ревізії = чинні в стані RAG", stale == 0 and set(revs) == with_text,
      f"розбіжностей {stale}, без тексту {len(active) - len(with_text)}, зайвих {len(set(revs) - with_text)}")
check("Qdrant: старих поколінь немає (retention)", other_gen == 0, f"{other_gen} точок")

# 3. Пошук знаходить лист за його темою; ACL, скринька, покоління, чужий job
random.seed(20261023)
sample = random.sample(sorted(active), 3)
found = 0
for doc_id in sample:
    d = active[doc_id]
    _, page = call(QDRANT + "/points/scroll", {"limit": 1, "with_payload": ["subject"], "with_vector": False, "filter": {"must": [
        {"key": "document_id", "match": {"value": doc_id}}, {"key": "generation", "match": {"value": GEN}},
        {"key": "chunk", "match": {"value": 0}}]}}, {"api-key": QKEY})
    subject = (page["result"]["points"] or [{"payload": {}}])[0]["payload"].get("subject") or doc_id
    user = d["user_ids"][0]
    status, res, job_id = search(subject, user, [d["mailbox_id"]])
    hits = res.get("hits", []) if status == 200 else []
    ok = any(h["document_id"] == doc_id and h["document_revision"] == d["revision"] for h in hits)
    found += ok
    if doc_id == sample[0]:
        check("пошук: хіт має id, ревізію, content_version і уривок",
              all({"document_id", "document_revision", "content_version", "text"} <= set(h) for h in hits) and bool(hits))
        s2, r2, _ = search(subject, max(d["user_ids"]) + 100000, [d["mailbox_id"]])
        check("ACL: інший користувач не бачить лист", s2 == 200 and not any(h["document_id"] == doc_id for h in r2.get("hits", [])))
        s3, r3, _ = search(subject, user, [d["mailbox_id"] + 100000])
        check("ACL: інша скринька — лист не повертається", s3 == 200 and not r3.get("hits"))
        s4, r4, _ = search(subject, user, [d["mailbox_id"]], generation=str(uuid.uuid4()))
        check("старе/чуже покоління → 409 generation_mismatch", s4 == 409 and r4.get("code") == "generation_mismatch")
        st5, r5 = call(BASE + f"/v2/jobs/{job_id}?source_instance_id={SRC}&generation={GEN}&user_id={user + 100000}", headers=AUTH)
        check("чужий job → 404 (як неіснуючий)", st5 == 404 and r5.get("code") == "not_found")
        st6, _ = call(BASE + f"/v2/capabilities?source_instance_id={SRC}&generation={GEN}&user_id=1",
                      headers={"Authorization": "Bearer wrong-" + uuid.uuid4().hex})
        check("чужий ключ → 401", st6 == 401)
check("пошук за темою знаходить сам лист (3 випадкові листи)", found == 3, f"{found}/3")

# 4. Публічна частина не бачить пошту
public_key = Path(r"D:\RAG\qdrant\qdrant.env").read_text(encoding="utf-8-sig").strip().split("=", 1)[1]
st7, cols = call("http://127.0.0.1:6333/collections", headers={"api-key": public_key})
names = [c["name"] for c in (cols.get("result") or {}).get("collections", [])] if st7 == 200 else []
check("публічний Qdrant (даташити) не має колекції пошти", st7 == 200 and "crm_correspondence" not in names, f"колекцій {len(names)}")
try:
    ctx = ssl.create_default_context()
    req = urllib.request.Request("https://app.sevskiy.com/v2/capabilities?source_instance_id=x&generation=y&user_id=1")
    with urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)).open(req, timeout=20) as r:
        code = r.status
except urllib.error.HTTPError as e:
    code = e.code
except Exception as e:  # noqa: BLE001
    code = type(e).__name__
check("/v2 з адреси не-Minerva через app.sevskiy.com → 403", code == 403, f"відповідь {code}")

print(json.dumps({"passed": sum(ok for _, ok, _ in results), "total": len(results), "at": time.strftime("%Y-%m-%d %H:%M:%S")}))
