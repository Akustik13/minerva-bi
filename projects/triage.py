"""«Розбір пошти»: гілки без проєкту за період, згруповані за клієнтом / доменом співрозмовника.

У групі гілки з однаковою темою (без Re:/AW:/WG:) стоять поруч і позначені — це кандидати в один проєкт.
Показуються лише скриньки поточного користувача.
"""
from collections import Counter, OrderedDict
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

TRIAGE_CATEGORIES = ("customer", "supplier", "other", "internal")
MAX_THREADS = 600


def user_accounts(user):
    from email_assistant.models import EmailAccount
    return list(EmailAccount.objects.filter(user=user, is_active=True))


def build_groups(accounts, days=90, q=""):
    from email_assistant.crm_link import FREE_MAIL_DOMAINS, is_own, own_addresses
    from email_assistant.models import EmailThread
    from email_assistant.sync import normalize_subject

    from .models import Project, ProjectLink
    from .services import suggest_name

    if not accounts:
        return [], 0
    since = timezone.now() - timedelta(days=days)
    qs = (EmailThread.objects
          .filter(account__in=accounts, last_message_at__gte=since, is_archived=False)
          .exclude(project_links__status=ProjectLink.CONFIRMED)
          .exclude(project_dismissal__isnull=False)
          .filter(messages__category__in=TRIAGE_CATEGORIES, messages__is_deleted=False, messages__is_spam=False))
    if q:
        qs = qs.filter(Q(subject__icontains=q) | Q(crm_customer__name__icontains=q) | Q(participants__icontains=q))
    qs = qs.distinct()
    total = qs.count()
    threads = list(qs.select_related("crm_customer").order_by("-last_message_at")[:MAX_THREADS])

    suggestions = {}
    for l in (ProjectLink.objects.filter(thread__in=threads, status=ProjectLink.SUGGESTED)
              .select_related("project")):
        suggestions.setdefault(l.thread_id, []).append(l)

    own = own_addresses()
    groups = OrderedDict()
    for t in threads:
        if t.crm_customer_id:
            key, label, customer = f"c{t.crm_customer_id}", t.crm_customer.name, t.crm_customer
        else:
            external = [p.lower() for p in (t.participants or []) if p and "@" in p and not is_own(p, own)]
            first = external[0] if external else ""
            domain = first.split("@", 1)[1] if first else ""
            # Gmail та інші безкоштовні пошти — групуємо за адресою, а не за доменом
            ident = first if domain in FREE_MAIL_DOMAINS else domain
            key, label, customer = f"d{ident or '-'}", ident or "(лише внутрішнє листування)", None
        g = groups.setdefault(key, {"key": key, "label": label, "customer": customer, "threads": []})
        g["threads"].append({"t": t, "core": normalize_subject(t.subject),
                             "suggestions": suggestions.get(t.pk, [])})

    for g in groups.values():
        sizes = Counter(item["core"] for item in g["threads"] if item["core"])
        for item in g["threads"]:
            item["cluster"] = sizes.get(item["core"], 0) if sizes.get(item["core"], 0) > 1 else 0
        # Однакові теми — поруч (найбільші групи першими), далі — за датою
        g["threads"].sort(key=lambda i: (-i["cluster"], i["core"] if i["cluster"] else "",
                                         -(i["t"].last_message_at.timestamp() if i["t"].last_message_at else 0)))
        biggest = [i["t"] for i in g["threads"] if i["cluster"] and i["core"] == g["threads"][0]["core"]]
        g["name_suggestion"] = suggest_name(biggest or [g["threads"][0]["t"]])
        g["projects"] = (list(Project.objects.filter(customer=g["customer"], status__in=Project.OPEN_STATUSES))
                         if g["customer"] else [])
    return list(groups.values()), total
