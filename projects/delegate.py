"""🤖 Доручити крок проєкту агенту (етап 5): готове доручення з контексту кроку → завдання агенту (agent.AgentTask).

presets(step) — варіанти: 📨 нагадати постачальнику чи клієнту (адреса — з прив'язаної закупівлі, учасників проєкту
або клієнта CRM), 🛒 підготувати закупівлю (товари проєкту), ✍️ своє завдання з контекстом кроку. Людина бачить і
змінює текст, адресу листа, модель і ліміт витрат; create створює AgentTask від її імені (право agent.add_agenttask),
прив'язує до кроку (ProjectStep.agent_tasks) і запускає (agent.tasks.start). Агент лише пропонує — лист,
закупівлю чи зміни застосовує людина на сторінці перевірки завдання.
"""
import re
from decimal import Decimal, InvalidOperation

from .models import ProjectParticipant, ProjectStep

BUDGET_MIN, BUDGET_MAX, BUDGET_DEFAULT = Decimal("0.05"), Decimal("10"), Decimal("0.50")
INSTRUCTION_MAX = 4000
TASKS_SHOWN = 5
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class DelegateError(ValueError):
    pass


def can(user) -> bool:
    return bool(user is not None and user.has_perm("agent.add_agenttask"))


def _due(step):
    from .schedule import planned_end
    end = planned_end(step) or step.f_end or step.end
    return f"{end:%d.%m.%Y}" if end else "—"


def _po(step):
    for link in step.links.select_related("purchase_order__supplier").exclude(purchase_order=None):
        return link.purchase_order
    return None


def _party(project, role):
    """(ім'я, адреса) учасника проєкту з цією роллю."""
    p = (ProjectParticipant.objects.filter(project=project, role=role).exclude(email="").order_by("pk").first())
    return (p.name or p.email, p.email) if p else ("", "")


def contacts(step) -> dict:
    """Кому писати: постачальник (закупівля кроку → учасник-постачальник), клієнт (клієнт CRM → учасник-клієнт)."""
    project = step.project
    po = _po(step)
    supplier = ("", "")
    if po is not None and po.supplier_id and po.supplier.email:
        supplier = (po.supplier.name, po.supplier.email)
    if not supplier[1]:
        supplier = _party(project, ProjectParticipant.SUPPLIER)
    customer = ("", "")
    if project.customer_id and project.customer.email:
        customer = (customer_label(project), project.customer.email)
    if not customer[1]:
        customer = _party(project, ProjectParticipant.CUSTOMER)
    return {"supplier": supplier, "customer": customer, "po": po}


def customer_label(project) -> str:
    c = project.customer if project.customer_id else None
    return (c.company or c.name or c.email) if c is not None else ""


def context_line(step) -> str:
    project = step.project
    customer = f", клієнт {customer_label(project)}" if project.customer_id else ""
    phase = f", фаза «{step.phase.name}»" if step.phase_id else ""
    return (f"Проєкт {project.code} «{project.name}»{customer}. Крок «{step.title}»{phase}: робить — "
            f"{step.get_side_display().lower()}, мав закінчитися {_due(step)}, стан — "
            f"{dict(ProjectStep.STATUS_CHOICES).get(step.eff_status or step.status, step.status)}.")


def presets(step) -> list:
    """Варіанти доручення [{key, label, instruction, mail_to}] — перший підходить кроку найкраще."""
    project, c = step.project, contacts(step)
    ctx = context_line(step)
    out = []
    name, email = c["supplier"]
    if email and step.side in (ProjectStep.SUPPLIER, ProjectStep.WE):
        po = c["po"]
        po_part = (f" Закупівля {po.code or po.pk}" + (f", очікували {po.expected_date:%d.%m.%Y}" if po.expected_date
                                                      else "") + ".") if po is not None else ""
        out.append({"key": "remind_supplier", "label": f"📨 Нагадати постачальнику {name}", "mail_to": email,
                    "instruction": f"{ctx}{po_part}\nПідготуй лист-нагадування постачальнику {name} ({email}): "
                                   f"попроси підтвердити дату відвантаження й трек-номер. Мова листа — як у нашому "
                                   f"останньому листуванні з ним, інакше англійська. Лист лише запропонуй — "
                                   f"надішлю я сам."})
    name, email = c["customer"]
    if email and step.side in (ProjectStep.CUSTOMER, ProjectStep.WE):
        out.append({"key": "remind_customer", "label": f"📨 Нагадати клієнту {name}", "mail_to": email,
                    "instruction": f"{ctx}\nПідготуй ввічливий лист клієнту {name} ({email}): нагадай про цей крок і "
                                   f"попроси відповісти, коли буде. Мова — як у листуванні з клієнтом. Лист лише "
                                   f"запропонуй — надішлю я сам."})
    if step.side == ProjectStep.SUPPLIER or step.trigger in ("po_ordered", "po_received", "jlc_ordered"):
        skus = ", ".join(project.products.values_list("sku", flat=True)[:15]) or "— (у проєкті не вказано)"
        supplier = f" Постачальник: {c['supplier'][0]} ({c['supplier'][1]})." if c["supplier"][1] else ""
        out.append({"key": "purchase", "label": "🛒 Підготувати закупівлю", "mail_to": "",
                    "instruction": f"{ctx}\nПідготуй закупівлю (замовлення постачальнику) для цього кроку. Товари "
                                   f"проєкту: {skus}.{supplier} Кількості візьми із замовлень і Angebote проєкту; "
                                   f"постачальника — з минулих закупівель цих товарів, якщо його не названо. "
                                   f"Закупівлю лише запропонуй — створю її я після перевірки."})
    out.append({"key": "custom", "label": "✍️ Своє завдання", "mail_to": "",
                "instruction": f"{ctx}\nЩо зробити: "})
    return out


def tasks_for(step) -> list:
    return [{"id": t.pk, "title": t.title, "status": t.status, "label": t.get_status_display(),
             "url": f"/admin/agent/agenttask/{t.pk}/review/"}
            for t in step.agent_tasks.order_by("-created_at", "-pk")[:TASKS_SHOWN]]


def _budget(value) -> Decimal:
    try:
        b = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return BUDGET_DEFAULT
    return max(BUDGET_MIN, min(BUDGET_MAX, b)).quantize(Decimal("0.01"))


def create(step, user, data):
    """Завдання агенту від імені людини, прив'язане до кроку, — і запуск у фоні. → AgentTask."""
    from agent import tasks
    from agent.models import DEFAULT_MODEL, TASK_MODELS, AgentTask
    if not can(user):
        raise DelegateError("Немає права давати завдання агенту")
    instruction = str(data.get("instruction") or "").strip()[:INSTRUCTION_MAX]
    if len(instruction) < 15 or instruction.rstrip().endswith("Що зробити:"):
        raise DelegateError("Напишіть, що має зробити агент")
    mail_to = ", ".join(dict.fromkeys(a.lower() for a in EMAIL_RE.findall(str(data.get("mail_to") or ""))))[:500]
    model = data.get("model") if data.get("model") in dict(TASK_MODELS) else DEFAULT_MODEL
    task = AgentTask.objects.create(owner=user, title=f"📁 {step.project.code} · {step.title}"[:200],
                                    instruction=instruction, mail_to=mail_to, model=model,
                                    budget_usd=_budget(data.get("budget")))
    products = list(step.project.products.values_list("pk", flat=True)[:20])
    if products:
        task.products.set(products)
    step.agent_tasks.add(task)
    tasks.start(task)
    return task


def editor(step, user) -> dict:
    """Для вікна «🤖 Доручити агенту»: варіанти, моделі, ліміт, завдання кроку."""
    from agent.models import DEFAULT_MODEL, TASK_MODELS
    return {"ok": True, "step": {"id": step.pk, "title": step.title}, "presets": presets(step),
            "models": TASK_MODELS, "model": DEFAULT_MODEL, "budget": str(BUDGET_DEFAULT), "tasks": tasks_for(step),
            "can": can(user)}
