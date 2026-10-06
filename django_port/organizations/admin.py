from django.contrib import admin

from .models import Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = (
        "name", "abbr", "country", "default_currency", "create_chart_of_accounts_based_on",
        "chart_of_accounts", "default_finance_book", "cost_center", "default_warehouse",
        "default_inventory_account", "stock_adjustment_account", "default_receivable_account",
        "default_payable_account", "is_group", "parent_company"
    )
    list_filter = ("country", "default_currency", "enable_perpetual_inventory", "is_group", "create_chart_of_accounts_based_on")
    search_fields = ("name", "abbr", "tax_id")
    raw_id_fields = ("existing_company", "parent_company")
