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

urlpatterns = [
    path("",           dashboard,       name="dashboard"),
    path("analytics/", analytics_index, name="analytics_index"),
    path("trends/",    trends_view,     name="trends"),
    path("bots/",      bots_index,      name="bots_index"),
    path("system/",    system_index,    name="system_index"),
    path("faq/",       faq_index,       name="faq_index"),
    path("help/",      help_page,       name="help"),
    path("signals/",       signals_page,  name="signals"),
    path("signals-count/", signals_count, name="signals_count"),
    path("import-template/<str:name>/", download_import_template, name="import_template"),
    path("import/", import_hub, name="import_hub"),
    path("api/",         api_index,   name="api_index"),
    path("api/console/", api_console, name="api_console"),
    path("api/proxy/",   api_proxy,   name="api_proxy"),
]
