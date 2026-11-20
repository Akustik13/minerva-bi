"""📊 Статистика запиту постачальникам (RFQ) — над таблицею пропозицій (suppliers/admin.py work_view).

  timing(r, letters)  — для рядка: за скільки відповів (перша відповідь − надсилання), останній лист постачальника,
                        скільки листів у листуванні (лише кількість і дати — текст листів тут не потрібен);
  stats(req, table)   — скільки запитали / відповіли / з цінами / відмовились / мовчать (з них строк минув),
                        час відповіді (медіана, найшвидший, найповільніший), по кожній кількості — найнижча, медіана
                        й найвища ціна за 1 шт. з разовими у валюті Minerva (хто), розкид; строк — найкоротший /
                        найдовший. Ціни — лише повні пропозиції (як 🥇); немає повних — з неповних, позначено.
  human(seconds)      — «45 хв» · «3 год 20 хв» · «2 дн 4 год».
"""
import statistics
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone


def human(seconds) -> str:
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    if s < 3600:
        return f"{max(1, s // 60)} хв"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h} год {m} хв" if m else f"{h} год"
    d, h = divmod(s // 3600, 24)
    return f"{d} дн {h} год" if h else f"{d} дн"


def timing(r, letters=None) -> dict:
    """{after, after_text, last_in, newer, letters_n}: after — секунд від надсилання до першої відповіді;
    last_in — найновіший лист постачальника (newer — він пізніший за першу відповідь)."""
    from suppliers.services import rfq_replies
    after = None
    if r.sent_at and r.replied_at and r.replied_at >= r.sent_at:
        after = (r.replied_at - r.sent_at).total_seconds()
    if letters is None:
        letters = rfq_replies.mail(r) if (r.thread_id or r.reply_message_id or r.sent_message_id) else []
    own = rfq_replies._own()
    ins = [m.sent_at for m in letters if m.sent_at and rfq_replies.theirs(m, own)]
    last_in = max(ins) if ins else None
    return {"after": after, "after_text": human(after), "last_in": last_in,
            "newer": bool(last_in and r.replied_at and (last_in - r.replied_at).total_seconds() > 60),
            "letters_n": len(letters), "in_n": len(ins)}


def _median(values):
    return statistics.median(values) if values else None


def stats(req, table) -> dict:
    from suppliers.models import SupplierRequestRecipient as R
    from suppliers.services import rfq
    from suppliers.services.rfq_compare import price
    rows = table["rows"]
    recips = [row["recipient"] for row in rows]
    sent = [r for r in recips if r.state not in (R.PLANNED, R.FAILED)]
    replied = [r for r in sent if r.replied_at or r.state in (R.REPLIED, R.QUOTED, R.DECLINED)]
    overdue = rfq.waiting_overdue(req)
    out = {
        "asked": len(sent), "planned": sum(1 for r in recips if r.state == R.PLANNED),
        "failed": sum(1 for r in recips if r.state == R.FAILED),
        "replied": len(replied), "replied_pct": round(100 * len(replied) / len(sent)) if sent else 0,
        "priced": sum(1 for row in rows if row["priced"]),
        "comparable": sum(1 for row in rows if row["comparable"]),
        "declined": sum(1 for r in recips if r.state == R.DECLINED),
        "silent": sum(1 for r in sent if r.state in (R.SENT, R.SILENT) and not r.replied_at),
        "overdue": len(overdue),
    }
    # ⏱ за скільки відповідали
    times = [(row["timing"]["after"], row["recipient"]) for row in rows if row.get("timing", {}).get("after") is not None]
    if times:
        fast = min(times, key=lambda x: x[0])
        slow = max(times, key=lambda x: x[0])
        out["reply"] = {"median": human(_median([t for t, _r in times])), "fast": human(fast[0]),
                        "fast_who": fast[1].supplier.name, "slow": human(slow[0]), "slow_who": slow[1].supplier.name,
                        "n": len(times)}
    if req.sent_at and out["silent"]:
        out["waiting_for"] = human((timezone.now() - req.sent_at).total_seconds())
    # 💶 ціни по кількостях — повні пропозиції (як 🥇); немає повних — з неповних, з позначкою
    per_qty = []
    for i, q in enumerate(table["qtys"]):
        pool = [row for row in rows if row["comparable"] and row["cells"][i].get("eff_home") is not None]
        partial = False
        if not pool:
            pool = [row for row in rows if row["priced"] and row["cells"] and row["cells"][i].get("eff_home") is not None]
            partial = bool(pool)
        if not pool:
            continue
        vals = sorted(((row["cells"][i]["eff_home"], row["recipient"].supplier.name) for row in pool),
                      key=lambda x: x[0])
        lo, hi = vals[0], vals[-1]
        spread = ""
        if lo[0] and hi[0] > lo[0]:
            times_ = (hi[0] / lo[0]).quantize(Decimal("0.1"), ROUND_HALF_UP)
            spread = f"×{str(times_).replace('.', ',')}"
        per_qty.append({"qty": q, "n": len(vals), "min": price(lo[0]), "min_who": lo[1], "max": price(hi[0]),
                        "max_who": hi[1], "median": price(Decimal(_median([v for v, _n in vals]))),
                        "spread": spread, "partial": partial, "one": len(vals) == 1})
    out["per_qty"] = per_qty
    out["home"] = table["home"]
    leads = [(row["recipient"].lead_days, row["recipient"].supplier.name) for row in rows
             if row["priced"] and row["recipient"].lead_days is not None]
    if leads:
        lo, hi = min(leads), max(leads)
        out["lead"] = {"min": lo[0], "min_who": lo[1], "max": hi[0], "max_who": hi[1], "n": len(leads)}
    return out
