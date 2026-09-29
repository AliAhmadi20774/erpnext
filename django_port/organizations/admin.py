from django.contrib import admin

from .models import Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "abbr", "country", "default_currency", "default_finance_book", "cost_center", "default_receivable_account", "default_payable_account", "is_group", "parent_company")
    list_filter = ("country", "default_currency", "is_group")
    search_fields = ("name", "abbr", "tax_id")
