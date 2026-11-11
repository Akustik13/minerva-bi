"""Compare local LLMs in Ollama on meeting work (stage 0 measurement; stdlib only).

  python compare_llm.py [model ...]  > report.json

Per model: speed (load, prompt and generation tokens/s, share in VRAM), key moments from a
synthetic uk/de meeting with six planted items (found? quote verbatim?), and two free texts
(Ukrainian answer, German e-mail) for a human to read. All texts here are invented.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bench import moments  # noqa: E402

OLLAMA = "http://127.0.0.1:11434"
DEFAULT_MODELS = ["qwen2.5:7b", "qwen3.5:4b-q4_K_M", "qwen3.5:9b-q4_K_M"]

SEGMENTS = [
    ("R", 0.0, "Guten Morgen zusammen, danke, dass Sie sich die Zeit nehmen."),
    ("L", 4.1, "Доброго ранку! Я переключуся на українську з колегою, а з вами — німецькою, якщо можна."),
    ("R", 9.0, "Natürlich. Es geht um die Antennen AN220213-01C für unser Projekt in München."),
    ("L", 14.2, "Ja, wir haben Ihre Anfrage über 350 Stück bekommen."),
    ("R", 18.5, "Der Kunde braucht die Ware spätestens am 20. November in München, sonst steht die Montage."),
    ("L", 25.3, "Олено, скільки в нас зараз на складі цих антен?"),
    ("L", 29.0, "Ризик у тому, що на складі лише 120 штук, решта прийде з JLCPCB не раніше 25 листопада."),
    ("R", 36.4, "Können Sie eine Teillieferung machen? Das müsste ich noch mit unserem Einkauf klären."),
    ("L", 42.0, "Das prüfen wir intern und melden uns, wir wissen es heute noch nicht."),
    ("L", 47.5, "Добре, домовились: першу партію зі 120 штук відправляємо в п'ятницю через UPS."),
    ("R", 53.1, "Einverstanden. Und der Preis bleibt bei 12,40 Euro pro Stück?"),
    ("L", 57.8, "Ja, und ich schicke Ihnen morgen das aktualisierte Angebot mit Staffelpreisen."),
    ("L", 63.0, "Олено, підготуй, будь ласка, Commercial Invoice і пакувальний лист до четверга."),
    ("R", 68.4, "Perfekt, dann warten wir auf Ihr Angebot. Vielen Dank und bis bald."),
]

# planted moments: every group needs one alternative present in quote+text (lower case)
PLANTED = {
    "decision": [["п'ятниц", "freitag", "friday"], ["ups"]],
    "task": [["commercial invoice", "інвойс", "invoice"], ["четвер", "donnerstag", "thursday"]],
    "deadline": [["20. november", "20 листопада", "20 november", "20.11"]],
    "risk": [["120"], ["jlcpcb", "25 листопада", "25. november"]],
    "question": [["teillieferung", "часткову", "partial"]],
    "promise": [["angebot", "пропозиц", "offer"], ["morgen", "завтра", "tomorrow"]],
}

UA_TASK = ("Дай коротку відповідь українською (3–4 речення) для менеджера, лише з цих фактів: антена AN220213-01C, "
           "діапазон 698–960 і 1710–3800 МГц, роз'єм SMA male, на складі 120 шт., ціна 12,40 EUR за шт. від 100 шт., "
           "строк решти — після 25 листопада. Питання клієнта: чи можемо поставити 350 шт. до 20 листопада?")
DE_TASK = ("Schreibe eine kurze, höfliche E-Mail auf Deutsch an Herrn Weber (Kunde): Wir können bis Freitag 120 Stück "
           "AN220213-01C per UPS liefern, die restlichen 230 Stück kommen voraussichtlich nach dem 25. November. "
           "Frage, ob eine Teillieferung für ihn in Ordnung ist. Höchstens 6 Sätze, ohne Betreffzeile.")


def call(path, body=None, timeout=900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(OLLAMA + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def unload_all():
    for m in call("/api/ps").get("models", []):
        call("/api/generate", {"model": m["name"], "keep_alive": 0, "stream": False})
    for _ in range(30):
        if not call("/api/ps").get("models"):
            return
        time.sleep(1)


def chat(model, prompt, think, num_predict=400, num_ctx=8192):
    body = {"model": model, "stream": False, "keep_alive": "10m",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_predict": num_predict, "num_ctx": num_ctx}}
    if think:
        body["think"] = False
    r = call("/api/chat", body)
    return {
        "text": r["message"]["content"].strip(),
        "load_s": round(r.get("load_duration", 0) / 1e9, 1),
        "prompt_tokens": r.get("prompt_eval_count"),
        "prompt_tok_s": round(r["prompt_eval_count"] / (r["prompt_eval_duration"] / 1e9), 1) if r.get("prompt_eval_duration") else None,
        "eval_tok_s": round(r["eval_count"] / (r["eval_duration"] / 1e9), 1) if r.get("eval_duration") else None,
    }


def score(result):
    found = {}
    for kind, groups in PLANTED.items():
        hit = None
        for item in result["items"]:
            hay = moments.normalize(" ".join([item.get("quote", ""), item.get("text", ""), item.get("due", "")]))
            if all(any(moments.normalize(alt) in hay for alt in group) for group in groups):
                hit = item
                break
        found[kind] = {"found": hit is not None, "as": hit.get("kind") if hit else None}
    return found


def run(model):
    unload_all()
    think = moments.supports_thinking(OLLAMA, model)
    out = {"model": model, "think_off": think}
    long_prompt = "Fasse den folgenden Text in einem Satz zusammen.\n\n" + " ".join(s[2] for s in SEGMENTS) * 12
    out["speed"] = chat(model, long_prompt, think, num_predict=200)
    out["speed"].pop("text", None)
    ps = [m for m in call("/api/ps").get("models", []) if m.get("name") == model]
    if ps:
        size, vram = ps[0].get("size", 0), ps[0].get("size_vram", 0)
        out["memory"] = {"size_mb": round(size / 2**20), "vram_mb": round(vram / 2**20),
                         "gpu_share": round(100 * vram / size) if size else None, "context": ps[0].get("context_length")}
    transcript = {"segments": [{"ch": ch, "start": t, "end": t + 4, "text": text} for ch, t, text in SEGMENTS]}
    res = moments.extract(OLLAMA, transcript, model, title="Antennen AN220213-01C, München", out_lang="uk")
    planted = score(res)
    out["moments"] = {
        "metrics": res["metrics"],
        "planted_found": sum(1 for v in planted.values() if v["found"]),
        "planted": planted,
        "items": [{k: i[k] for k in ("kind", "text", "who", "due", "quote", "line")} for i in res["items"]],
        "rejected": [{k: i[k] for k in ("kind", "quote", "reason")} for i in res["rejected"]],
        "summary": res["summary"],
    }
    out["uk_answer"] = chat(model, UA_TASK, think)
    out["de_mail"] = chat(model, DE_TASK, think)
    return out


if __name__ == "__main__":
    models = sys.argv[1:] or DEFAULT_MODELS
    report = {"ollama": call("/api/version")["version"], "at": time.strftime("%Y-%m-%d %H:%M"), "runs": []}
    for model in models:
        try:
            report["runs"].append(run(model))
        except Exception as exc:  # noqa: BLE001 - one model failing must not lose the others
            report["runs"].append({"model": model, "error": repr(exc)[:500]})
    unload_all()
    json.dump(report, sys.stdout, ensure_ascii=False, indent=1)
