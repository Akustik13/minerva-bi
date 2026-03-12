from django.urls import path
from .views import dashboard
from .signals_views import signals_page
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
    context = admin.site.each_context(request)
    try:
        from config.models import DocumentSettings
        context['doc_settings'] = DocumentSettings.get()
    except Exception:
        context['doc_settings'] = None
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

urlpatterns = [
    path("",           dashboard,       name="dashboard"),
    path("analytics/", analytics_index, name="analytics_index"),
    path("bots/",      bots_index,      name="bots_index"),
    path("system/",    system_index,    name="system_index"),
    path("faq/",       faq_index,       name="faq_index"),
    path("help/",      help_page,       name="help"),
    path("signals/",   signals_page,    name="signals"),
    path("import-template/<str:name>/", download_import_template, name="import_template"),
    path("import/", import_hub, name="import_hub"),
    path("api/",         api_index,   name="api_index"),
    path("api/console/", api_console, name="api_console"),
    path("api/proxy/",   api_proxy,   name="api_proxy"),
]
