"""Key moments with a paid cloud model (Anthropic Claude) - only after consent for this recording.

Kept apart from moments.py (local Ollama, standard library only). Here the TRANSCRIPT TEXT leaves the RAG PC and
goes to Anthropic (USA); audio never does. The key is the RAG's stored Anthropic key (see keystore.py).

The same prompt and the same verbatim-quote check (moments.validate) as the local path, so results compare.
Claude 5.5 models: adaptive thinking (no "thinking" parameter), effort per model, JSON-schema output
(output_config.format), streaming for long input, no temperature. Opus / Sonnet go through the beta client with
the server-side refusal fallback (another Claude model continues if the first one declines), retried without it if
the account rejects the beta. The cost counts every attempt (usage.iterations) at its own model's price.
"""
import json
import time

from . import keystore, moments

PROVIDER_NAMES = {"anthropic": "Anthropic (Claude)"}
# id -> (label, effort, USD per 1M tokens: input, output, cache read, 5-minute cache write); pricing 10.11.2026
MODELS = {
    "claude-opus-5-5": ("Claude Opus 5.5 — найточніша", "medium", (4.00, 20.00, 0.20, 5.00)),
    "claude-sonnet-5-5": ("Claude Sonnet 5.5 — швидша, удвічі дешевша", "low", (2.00, 10.00, 0.10, 2.50)),
    "claude-haiku-5-5": ("Claude Haiku 5.5 — найдешевша", "medium", (0.10, 0.50, 0.01, 0.125)),
}
HAIKU_LONG = (0.50, 2.50, 0.05, 0.625)    # Haiku 5.5 when one request's prompt is over 100 000 tokens
# models the server-side fallback may answer with (never offered on the page): price only
FALLBACK_PRICES = {"claude-opus-5": (5.00, 25.00, 0.50, 6.25), "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
                   "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50)}
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 64000                         # thinking counts toward it; streaming allows it
TIMEOUT = 900.0
BASE_URL = "https://api.anthropic.com"     # fixed, like the RAG: never an ANTHROPIC_BASE_URL from the environment

KIND_LIST = list(moments.KINDS)
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": KIND_LIST},
                    "text": {"type": "string"},
                    "who": {"type": "string", "description": "responsible person or speaker, or empty string"},
                    "due": {"type": "string", "description": "date or deadline exactly as said, or empty string"},
                    "line": {"type": "integer", "description": "number N of the transcript line [#N]"},
                    "quote": {"type": "string", "description": "exact fragment copied from that line"},
                },
                "required": ["kind", "text", "who", "due", "line", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "items"],
    "additionalProperties": False,
}


class CloudError(Exception):
    """A message for the page (Ukrainian, no key, no transcript text) plus what the failed run cost."""

    def __init__(self, text, usage=None):
        super().__init__(text)
        self.usage = usage or {}


HOUR_TOKENS = (25000, 8000)  # one hour of meeting ≈ 25k prompt tokens; answer + thinking ≈ 8k (estimate)


def label(model):
    return MODELS.get(model, (model,))[0]


def hour_price(model):
    """Rough USD for one hour of meeting, for the model list; the real cost is shown after each run."""
    c = cost_usd(model, {"input_tokens": HOUR_TOKENS[0], "output_tokens": HOUR_TOKENS[1]})
    return round(c, 2) if c is not None else None


def choices(cfg, installed):
    """What the page offers: installed local models first, then paid ones (ready = a key is stored). No keys here."""
    out = []
    for m in cfg["llm_models"]:
        if m in installed:
            out.append({"value": f"ollama:{m}", "provider": "ollama", "model": m, "cloud": False, "ready": True,
                        "label": f"{m} — на RAG-ПК, безкоштовно"})
    for provider, models in (cfg.get("llm_cloud_models") or {}).items():
        if provider not in PROVIDER_NAMES or not isinstance(models, list):
            continue
        ready = bool(keystore.get(provider, cfg.get("secrets_file")))
        for m in [m for m in models if m in MODELS]:  # unknown ids: no price, no effort -> not offered
            price = hour_price(m)
            out.append({"value": f"{provider}:{m}", "provider": provider, "model": m, "cloud": True, "ready": ready,
                        "price_hour": price,
                        "label": f"{label(m)} — {PROVIDER_NAMES[provider]}, платно"
                                 + (f", ≈ ${price:.2f} за годину наради".replace(".", ",") if price is not None else "")
                                 + ("" if ready else " (ключ не збережено)")})
    return out


def default_choice(cfg, offered):
    """config llm_provider / llm_model if it can run now; else another local model; '' if none.

    A paid model is the default only when llm_provider names its provider: while Ollama restarts the local models
    are briefly not offered, and the page must not fall back to Claude Opus (review 10.11)."""
    provider = cfg.get("llm_provider") or "ollama"
    if provider == "ollama":
        model = cfg.get("llm_model") or cfg["llm_default"]
    else:
        model = cfg.get("llm_model") or ((cfg.get("llm_cloud_models") or {}).get(provider) or [""])[0]
    wanted = f"{provider}:{model}"
    ready = [c for c in offered if c["ready"]]
    if wanted in [c["value"] for c in ready]:
        return wanted
    same = [c["value"] for c in ready if c["provider"] == provider] if provider != "ollama" else []
    local = [c["value"] for c in ready if not c["cloud"]]
    return (same or local or [""])[0]


def effort_of(model, configured=None):
    return (configured or {}).get(model) or MODELS.get(model, (None, None))[1]


def price_of(model):
    if model in MODELS:
        return MODELS[model][2]
    return FALLBACK_PRICES.get(model)


def cost_usd(model, u):
    """USD for one attempt; None when the model's price is unknown (the page then shows «—», never 0)."""
    prompt = u.get("input_tokens", 0) + u.get("cache_write_tokens", 0) + u.get("cache_read_tokens", 0)
    price = HAIKU_LONG if model.startswith("claude-haiku-5-5") and prompt > 100_000 else price_of(model)
    if price is None:
        return None
    p_in, p_out, p_read, p_write = price
    return (u.get("input_tokens", 0) * p_in + u.get("output_tokens", 0) * p_out
            + u.get("cache_read_tokens", 0) * p_read + u.get("cache_write_tokens", 0) * p_write) / 1_000_000


def _field(obj, name):
    value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return (value or 0) if name.endswith("tokens") else value


def _usage(msg, asked):
    """Tokens and cost of one request. With a fallback the declined attempt is billed too: usage.iterations lists
    every attempt (top-level usage covers only the last one), each priced by its own model."""
    u = getattr(msg, "usage", None)
    served = getattr(msg, "model", "") or asked
    attempts = []
    for it in (getattr(u, "iterations", None) or []):
        attempts.append({"model": _field(it, "model") or asked, "type": _field(it, "type") or "",
                         "input_tokens": _field(it, "input_tokens"), "output_tokens": _field(it, "output_tokens"),
                         "cache_read_tokens": _field(it, "cache_read_input_tokens"),
                         "cache_write_tokens": _field(it, "cache_creation_input_tokens")})
    if not attempts:
        attempts = [{"model": served, "type": "message", "input_tokens": getattr(u, "input_tokens", 0) or 0,
                     "output_tokens": getattr(u, "output_tokens", 0) or 0,
                     "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                     "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}]
    out = {"model": served, "stop_reason": getattr(msg, "stop_reason", None),
           "fallback": any(a["type"] == "fallback_message" for a in attempts)}
    for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        out[k] = sum(a[k] for a in attempts)
    costs = [cost_usd(a["model"], a) for a in attempts]
    out["cost_usd"] = None if any(c is None for c in costs) else sum(costs)
    return out


def _overloaded(anthropic):
    """529 / 503 classes of the installed SDK (2.x has its own; older ones use InternalServerError)."""
    return tuple(getattr(anthropic, n) for n in ("OverloadedError", "ServiceUnavailableError") if hasattr(anthropic, n))


def _error_type(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        return err.get("type")
    return getattr(exc, "type", None)


def _final(manager):
    with manager as stream:
        return stream.get_final_message()


def _api_text(exc):
    """The API's own reason from an error body (not the SDK's 'Error code: 400 - {...}' repr)."""
    body = getattr(exc, "body", None)
    err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:200]
    return str(getattr(exc, "message", "") or exc)[:200]


def call(client, model, system, user_text, effort):
    """One request -> (dict or None when the JSON is cut off / unreadable / declined, usage). Raises CloudError."""
    import anthropic
    import httpx2  # the SDK's own HTTP library: a stream that breaks after HTTP 200 raises its errors

    output_config = {"format": {"type": "json_schema", "schema": SCHEMA}}
    if effort:
        output_config["effort"] = effort
    params = {"model": model, "max_tokens": MAX_TOKENS, "system": system,
              "messages": [{"role": "user", "content": user_text}], "output_config": output_config}
    try:
        if model in FALLBACK_MODELS:
            try:  # the beta client tracks the fallback model and every attempt's usage
                msg = _final(client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default"))
            except anthropic.BadRequestError:
                msg = _final(client.messages.stream(**params))  # the account or model without the fallback beta
        else:
            msg = _final(client.messages.stream(**params))
    except anthropic.AuthenticationError as exc:
        raise CloudError("Anthropic не прийняв ключ (недійсний або відкликаний). Перевірте ключ у «RAG — керування» → «🎙 Наради».") from exc
    except anthropic.PermissionDeniedError as exc:
        raise CloudError("Ключу Anthropic заборонено цей запит (дозволи організації).") from exc
    except anthropic.NotFoundError as exc:
        raise CloudError(f"Модель {model} недоступна для цього ключа Anthropic.") from exc
    except anthropic.RateLimitError as exc:
        raise CloudError("Ліміт запитів Anthropic вичерпано — спробуйте за хвилину.") from exc
    except anthropic.BadRequestError as exc:
        text = _api_text(exc)
        if "credit balance" in text.lower():
            raise CloudError("На рахунку Anthropic немає коштів — поповніть баланс у console.anthropic.com.") from exc
        raise CloudError(f"Anthropic відхилив запит: {text}") from exc
    except _overloaded(anthropic) as exc:
        raise CloudError("Anthropic зараз перевантажений — спробуйте за кілька хвилин.") from exc
    except anthropic.APIStatusError as exc:
        kind = _error_type(exc)
        if kind == "billing_error" or exc.status_code == 402:
            raise CloudError("На рахунку Anthropic немає коштів — поповніть баланс у console.anthropic.com.") from exc
        if kind == "overloaded_error" or exc.status_code == 529:
            raise CloudError("Anthropic зараз перевантажений — спробуйте за кілька хвилин.") from exc
        if exc.status_code == 413:
            raise CloudError("Розшифровка завелика для одного запиту — зменште «llm_cloud_chunk_chars».") from exc
        if exc.status_code == 200:  # an error event in the middle of the stream
            raise CloudError(f"Відповідь Anthropic обірвалась ({kind or 'помилка'}) — спробуйте ще раз.") from exc
        raise CloudError(f"Помилка API Anthropic {exc.status_code} — спробуйте пізніше.") from exc
    except anthropic.APITimeoutError as exc:
        raise CloudError("Anthropic не відповів вчасно — спробуйте ще раз.") from exc
    except anthropic.APIConnectionError as exc:
        raise CloudError("Немає з’єднання з api.anthropic.com — перевірте інтернет на RAG-ПК.") from exc
    except httpx2.TimeoutException as exc:
        raise CloudError("Anthropic не відповів вчасно — спробуйте ще раз.") from exc
    except httpx2.HTTPError as exc:
        raise CloudError("З’єднання з api.anthropic.com обірвалось посеред відповіді — спробуйте ще раз.") from exc
    usage = _usage(msg, model)
    if usage["stop_reason"] in ("refusal", "max_tokens"):
        return None, usage
    text = "".join(getattr(b, "text", "") for b in (msg.content or []) if getattr(b, "type", "") == "text")
    try:
        data = json.loads(text)
    except ValueError:
        data = moments._parse(text)
    return (data if isinstance(data, dict) else None), usage


def extract(provider, model, transcript, title="", out_lang="uk", chunk_chars=400000, effort=None,
            secrets_file=None, client=None):
    """Same contract as moments.extract: {'model', 'provider', 'lang', 'summary', 'items', 'rejected', 'metrics'}."""
    if provider != "anthropic":
        raise CloudError(f"Постачальник «{provider}» ще не підтримується.")
    if client is None:
        key = keystore.get("anthropic", secrets_file)
        if not key:
            raise CloudError("Ключ Anthropic не збережено — додайте його в «RAG — керування» → «🎙 Наради».")
        import anthropic

        client = anthropic.Anthropic(api_key=key, base_url=BASE_URL, max_retries=2,
                                     timeout=anthropic.Timeout(TIMEOUT, connect=15.0))
    lines = moments.lines_of(transcript)
    system = moments.SYSTEM.replace("{lang}", moments.LANG_NAMES.get(out_lang, "Ukrainian"))
    started = time.time()
    total = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    cost, served, stops = 0.0, set(), []
    summaries, items, raw_count, bad, refused, chunks, fallback = [], [], 0, 0, 0, 0, 0
    for batch in moments.chunks(lines, chunk_chars):
        user_text = f"Meeting: {title or '-'}\nTranscript:\n<<<TRANSCRIPT\n{moments.render(batch)}\nTRANSCRIPT>>>"
        try:
            data, usage = call(client, model, system, user_text, effort)
        except CloudError as exc:
            exc.usage = _sum(total, cost, exc.usage)
            raise
        chunks += 1
        for k in total:
            total[k] += usage.get(k, 0)
        cost = None if cost is None or usage.get("cost_usd") is None else cost + usage["cost_usd"]
        served.add(usage.get("model") or model)
        stops.append(usage.get("stop_reason"))
        fallback += bool(usage.get("fallback"))
        if usage.get("stop_reason") == "refusal":
            refused += 1  # keep the other pieces of a long meeting
            continue
        if not isinstance(data, dict):
            bad += 1
            continue
        if data.get("summary"):
            summaries.append(str(data["summary"]).strip())
        raw = data.get("items") or []
        raw_count += len(raw)
        items.extend(raw)
    if chunks and refused == chunks:
        raise CloudError("Claude відмовився обробляти цей текст.", _sum(total, cost, None))
    ok, rejected = moments.validate(items, lines)
    metrics = {
        "provider": provider, "model": model, "served_by": ", ".join(sorted(served)), "effort": effort,
        "seconds": round(time.time() - started, 1), "chunks": chunks, "bad_json": bad, "raw_items": raw_count,
        "items": len(ok), "rejected": len(rejected), "refused": refused, **total,
        "prompt_tokens": total["input_tokens"] + total["cache_read_tokens"] + total["cache_write_tokens"],
        "eval_tokens": total["output_tokens"], "cost_usd": None if cost is None else round(cost, 4),
        "stop_reasons": stops, "fallbacks": fallback,
    }
    return {"model": model, "provider": provider, "lang": out_lang, "summary": " ".join(summaries), "items": ok,
            "rejected": rejected, "metrics": metrics}


def _sum(total, cost, last):
    """Usage of the whole failed run (earlier pieces + the failing request) for the runs history."""
    out = dict(total)
    for k in out:
        out[k] += (last or {}).get(k, 0)
    last_cost = (last or {}).get("cost_usd", 0 if not last else None)
    out["cost_usd"] = None if cost is None or last_cost is None else round(cost + last_cost, 4)
    return out
