from django.urls import path
from .views import dashboard
from .signals_views import signals_page, signals_count
from django.shortcuts import render
from django.http import Http404, HttpResponse
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import admin

def help_page(request):
    context = admin.site.each_context(request)
    return render(request, 'dashboard/help.html', context)
help_page = staff_member_required(help_page)

def faq_index(request):
    context = admin.site.each_context(request)
    return render(request, 'dashboard/faq_index.html', context)
faq_index = staff_member_required(faq_index)

def home_page(request):
    return render(request, 'dashboard/home.html')

def analytics_index(request):
    context = admin.site.each_context(request)
    return render(request, 'dashboard/analytics_index.html', context)
analytics_index = staff_member_required(analytics_index)

def bots_index(request):
    context = admin.site.each_context(request)
    return render(request, 'dashboard/bots_index.html', context)
bots_index = staff_member_required(bots_index)

def ai_index(request):
    context = admin.site.each_context(request)
    try:
        from strategy.models import AISettings
        context['ai_key_ok'] = bool(AISettings.get().anthropic_api_key)
    except Exception:
        context['ai_key_ok'] = False
    try:
        from ai_assistant.models import AIBudgetLog, AIConversation
        budget = AIBudgetLog.current()
        context['ai_budget_used'] = float(budget.total_cost_usd)
        context['ai_budget_limit'] = float(AISettings.get().monthly_budget_usd)
        context['ai_conv_count'] = AIConversation.objects.filter(is_active=True).count()
    except Exception:
        context['ai_budget_used'] = None
        context['ai_budget_limit'] = None
        context['ai_conv_count'] = None
    return render(request, 'dashboard/ai_index.html', context)
ai_index = staff_member_required(ai_index)

def system_index(request):
    from django.utils import timezone
    context = admin.site.each_context(request)
    try:
        from config.models import DocumentSettings
        context['doc_settings'] = DocumentSettings.get()
    except Exception:
        context['doc_settings'] = None

    # ── System status data ──
    try:
        from backup.models import BackupLog
        last_bk = BackupLog.objects.filter(status='ok').order_by('-created_at').first()
        if last_bk:
            delta = timezone.now() - last_bk.created_at
            days = delta.days
            if days == 0:
                label = 'сьогодні'
            elif days == 1:
                label = 'вчора'
            else:
                label = f'{days} дн. тому'
            context['last_backup_label'] = label
            context['last_backup_days'] = days
        else:
            context['last_backup_label'] = 'ніколи'
            context['last_backup_days'] = 9999
    except Exception:
        context['last_backup_label'] = '?'
        context['last_backup_days'] = 9999

    try:
        from shipping.models import ShippingSettings
        sh = ShippingSettings.objects.filter(pk=1).first()
        context['auto_tracking'] = sh.auto_tracking_enabled if sh else False
    except Exception:
        context['auto_tracking'] = False

    try:
        from api.models import APIKey
        context['api_key_count'] = APIKey.objects.filter(is_active=True).count()
    except Exception:
        context['api_key_count'] = 0

    try:
        from sales.models import SalesOrder
        context['unshipped_count'] = SalesOrder.objects.filter(
            affects_stock=True, shipped_at__isnull=True
        ).count()
    except Exception:
        context['unshipped_count'] = 0

    return render(request, 'dashboard/system_index.html', context)
system_index = staff_member_required(system_index)

def import_hub(request):
    context = admin.site.each_context(request)
    return render(request, 'dashboard/import_hub.html', context)
import_hub = staff_member_required(import_hub)


def download_import_template(request, name):
    from .import_templates import build_inventory_template, build_sales_template
    CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name == "inventory":
        wb = build_inventory_template()
        filename = "minerva_inventory_import.xlsx"
    elif name == "sales":
        wb = build_sales_template()
        filename = "minerva_sales_import.xlsx"
    else:
        raise Http404
    response = HttpResponse(content_type=CONTENT_TYPE)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response
download_import_template = staff_member_required(download_import_template)


from .api_views import api_index, api_console, api_proxy
from .views import trends_view


def widget_data_api(request):
    """Per-user widget data for the home dashboard panel."""
    from django.http import JsonResponse
    from datetime import date
    from django.utils import timezone as _tz
    data = {}
    today = date.today()

    # ── Email ──────────────────────────────────────────────────────────────
    try:
        from email_assistant.models import EmailMessage, EmailThread, EmailAccount
        em_account = EmailAccount.objects.filter(
            user=request.user, is_active=True
        ).order_by('-is_primary').first()
        if em_account:
            from django.db.models import Q
            archived_ids = EmailThread.objects.filter(
                account=em_account, is_archived=True
            ).values_list('id', flat=True)
            data['unread_emails'] = EmailMessage.objects.filter(
                account=em_account,
                folder='inbox',
                is_read=False, is_deleted=False,
            ).filter(
                Q(imap_folder_name='') | Q(imap_folder_name__iexact=em_account.imap_folder_inbox)
            ).exclude(thread_id__in=archived_ids).count()
            data['unread_threads'] = EmailThread.objects.filter(
                account=em_account, has_unread=True, is_archived=False
            ).count()
        else:
            data['unread_emails'] = 0
            data['unread_threads'] = 0
    except Exception:
        data['unread_emails'] = 0
        data['unread_threads'] = 0

    # ── Sales ──────────────────────────────────────────────────────────────
    try:
        from sales.models import SalesOrder
        data['new_orders']        = SalesOrder.objects.filter(status='received').count()
        data['processing_orders'] = SalesOrder.objects.filter(status='processing').count()
        data['sales_today']       = SalesOrder.objects.filter(order_date=today).count()
        data['unshipped']         = SalesOrder.objects.filter(
            affects_stock=True, shipped_at__isnull=True,
            status__in=['received', 'processing']
        ).count()
    except Exception:
        data['new_orders'] = 0
        data['processing_orders'] = 0
        data['sales_today'] = 0
        data['unshipped'] = 0

    # ── Shipping ───────────────────────────────────────────────────────────
    try:
        from shipping.models import Shipment
        data['in_transit'] = Shipment.objects.filter(status='in_transit').count()
    except Exception:
        data['in_transit'] = 0

    # ── Inventory (inline — no admin import) ───────────────────────────────
    try:
        from django.db.models import Sum, OuterRef, Subquery, F, Value
        from django.db.models.functions import Coalesce
        from decimal import Decimal
        from inventory.models import Product, InventoryTransaction, PurchaseOrder
        stock_subq = (
            InventoryTransaction.objects
            .filter(product=OuterRef('pk'))
            .values('product')
            .annotate(total=Sum('qty'))
            .values('total')
        )
        data['critical_stock'] = (
            Product.objects.filter(is_active=True, reorder_point__gt=0)
            .annotate(stock=Coalesce(Subquery(stock_subq), Value(Decimal('0'))))
            .filter(stock__lt=F('reorder_point'))
            .count()
        )
        data['active_po'] = PurchaseOrder.objects.filter(
            status__in=['draft', 'ordered', 'partial']
        ).count()
    except Exception:
        data['critical_stock'] = 0
        data['active_po'] = 0

    # ── Tasks ──────────────────────────────────────────────────────────────
    try:
        from tasks.models import Task
        data['tasks_pending'] = Task.objects.filter(
            status__in=['pending', 'in_progress']
        ).count()
    except Exception:
        data['tasks_pending'] = 0

    # ── Calendar ───────────────────────────────────────────────────────────
    try:
        from calendar_app.models import CalendarEvent
        now = _tz.now()
        d_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        d_end   = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        data['calendar_today'] = CalendarEvent.objects.filter(
            user=request.user, start_at__gte=d_start, start_at__lte=d_end,
        ).count()
    except Exception:
        data['calendar_today'] = 0

    return JsonResponse(data)
widget_data_api = staff_member_required(widget_data_api)


urlpatterns = [
    path("",           dashboard,       name="dashboard"),
    path("analytics/", analytics_index, name="analytics_index"),
    path("trends/",    trends_view,     name="trends"),
    path("bots/",      bots_index,      name="bots_index"),
    path("ai/",        ai_index,        name="ai_index"),
    path("system/",    system_index,    name="system_index"),
    path("faq/",       faq_index,       name="faq_index"),
    path("help/",      help_page,       name="help"),
    path("signals/",       signals_page,  name="signals"),
    path("signals-count/", signals_count, name="signals_count"),
    path("import-template/<str:name>/", download_import_template, name="import_template"),
    path("import/", import_hub, name="import_hub"),
    path("api/",              api_index,       name="api_index"),
    path("api/console/",      api_console,     name="api_console"),
    path("api/proxy/",        api_proxy,       name="api_proxy"),
    path("api/widget-data/",  widget_data_api, name="widget_data_api"),
]
