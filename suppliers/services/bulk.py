"""
suppliers/services/bulk.py — дії для кількох постачальників одразу (список «📦 Склад → Постачальники» → дії
«🧵 Зібрати історію з пошти…», «🧠 AI-оцінка з листування…», «🌐 Знайти про них в інтернеті…» — сторінка
…/supplier/bulk/, і агент — propose_supplier_check у agent/tools_suppliers.py). По одному постачальнику, хід —
report(«3 / 16 · PCBWay»); підсумок по кожному — rows [{name, url, text}].

  history(pks, user, date_from, date_to, make_po, as_history, report) — 🧵 пошта людини → теми, події, контакти
                                                                        (history.rebuild), за бажанням — закупівлі
                                                                        з PI (history.make_purchases);
  ai(do, pks, user, model, report)                                    — 🧠 evaluate (mailscan + закупівлі) /
                                                                        🌐 research; три збої AI поспіль — стоп.
"""
from django.urls import reverse

AI_MAX = 20               # 🧠 / 🌐 за один запуск — платно, кожен постачальник — окремий запит AI
HISTORY_MAX = 100
STOP_AFTER = 3


def _url(s) -> str:
    return reverse("admin:inventory_supplier_change", args=[s.pk])


def history(pks, user, date_from, date_to, make_po=False, as_history=True, report=None) -> dict:
    from inventory.models import Supplier
    from suppliers.services import history as hist
    pks = list(pks)[:HISTORY_MAX]
    report = report or (lambda _text: None)
    rows, tot = [], {"messages": 0, "topics": 0, "contacts": 0, "pos": 0}
    for i, s in enumerate(Supplier.objects.filter(pk__in=pks).order_by("name"), 1):
        report(f"{i} / {len(pks)} · {s.name}")
        out = hist.rebuild(s, user, date_from=date_from, date_to=date_to)
        made = hist.make_purchases(s, user, as_history=as_history)["pos"] if make_po else []
        for k in ("messages", "topics", "contacts"):
            tot[k] += out.get(k) or 0
        tot["pos"] += len(made)
        rows.append({"name": s.name, "url": _url(s),
                     "text": f"листів {out['messages']}, тем {out['topics']}"
                             + (f", 👥 +{out['contacts']}" if out.get("contacts") else "")
                             + (f", 🛒 {', '.join(p.code for p in made)}" if made else "")
                             + (" · ⚠️ понад 1500 листів" if out.get("truncated") else "")})
    return {"message": f"🧵 Готово: постачальників {len(rows)}, листів {tot['messages']}, тем "
                       f"{tot['topics']}, нових контактних осіб {tot['contacts']}"
                       + (f", закупівель {tot['pos']}" if make_po else ""), "rows": rows}


def ai(do, pks, user, model, report=None) -> dict:
    from inventory.models import Supplier
    from suppliers.services import ai as sai
    from suppliers.services import mailscan
    from suppliers.services.overview import purchases
    if do not in ("evaluate", "research"):
        raise ValueError(do)
    pks = list(pks)[:AI_MAX]
    report = report or (lambda _text: None)
    rows, done, streak = [], 0, 0
    for i, s in enumerate(Supplier.objects.filter(pk__in=pks).order_by("name"), 1):
        report(f"{i} / {len(pks)} · {s.name}")
        try:
            if do == "evaluate":
                a = sai.evaluate(s, user, mailscan.scan(s, user), purchases(s), model=model)
                text = f"оцінка {a.result.get('rating') or '—'}/5"
            else:
                sai.research(s, user=user, model=model)
                text = "✓ знайдено"
            done, streak = done + 1, 0
        except sai.AIError as e:
            text, streak = f"⚠️ {e}", streak + 1
        rows.append({"name": s.name, "url": _url(s), "text": text})
        if streak >= STOP_AFTER:
            rows.append({"name": "", "url": "", "text": "⏹ Зупинено після трьох помилок поспіль."})
            break
    return {"message": f"{'🧠' if do == 'evaluate' else '🌐'} Готово: {done} з {len(pks)}", "rows": rows}
