from django.contrib import admin
from django.shortcuts import render
from django.urls import path
from .models import FaqPlaceholder


@admin.register(FaqPlaceholder)
class FaqPlaceholderAdmin(admin.ModelAdmin):
    """Статична інформаційна сторінка — таблиці в БД немає."""

    def get_urls(self):
        return [
            path("", self.admin_site.admin_view(self.info_view),
                 name="faq_faqplaceholder_changelist"),
        ]

    def info_view(self, request):
        ctx = dict(
            self.admin_site.each_context(request),
            title="❓ FAQ та підтримка",
            opts=self.model._meta,
        )
        return render(request, "admin/faq/info.html", ctx)

    def has_add_permission(self, request):          return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False
    def has_view_permission(self, request, obj=None):   return True
