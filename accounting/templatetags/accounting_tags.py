from django import template
from django.db.models import Count

register = template.Library()


@register.simple_tag
def invoice_stats():
    """Returns a dict with invoice counts by status."""
    try:
        from accounting.models import Invoice
        qs = Invoice.objects.values("status").annotate(n=Count("pk"))
        by_status = {row["status"]: row["n"] for row in qs}
        return {
            "total":   sum(by_status.values()),
            "sent":    by_status.get("sent", 0),
            "overdue": by_status.get("overdue", 0),
            "draft":   by_status.get("draft", 0),
            "paid":    by_status.get("paid", 0),
        }
    except Exception:
        return {"total": "—", "sent": "—", "overdue": "—", "draft": "—", "paid": "—"}
