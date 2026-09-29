from django.contrib import admin

from .models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "project_name", "company", "status", "is_active", "customer")
    list_filter = ("company", "status", "is_active")
    search_fields = ("name", "project_name", "customer__name")
