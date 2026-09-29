from django.contrib import admin

from .models import Bin, StockLedgerEntry, Warehouse, WarehouseType


@admin.register(WarehouseType)
class WarehouseTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name", "description")


@admin.register(Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "company",
        "parent_warehouse",
        "is_group",
        "warehouse_type",
        "account",
        "disabled",
        "lft",
        "rgt",
    )
    list_filter = ("company", "is_group", "disabled", "warehouse_type", "is_rejected_warehouse")
    search_fields = ("name", "warehouse_name", "city", "customer__name")
    readonly_fields = ("name", "lft", "rgt")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["warehouse_name", "company"] if obj else [])

    def delete_queryset(self, request, queryset):
        for warehouse in queryset.order_by("company_id", "-lft"):
            warehouse.delete()


@admin.register(Bin)
class BinAdmin(admin.ModelAdmin):
    list_display = (
        "item", "warehouse", "company", "actual_qty", "projected_qty",
        "valuation_rate", "stock_value",
    )
    list_filter = ("company", "warehouse")
    search_fields = ("item__name", "item__item_name", "warehouse__name")
    readonly_fields = tuple(field.name for field in Bin._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockLedgerEntry)
class StockLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "name", "posting_datetime", "item", "warehouse", "actual_qty",
        "qty_after_transaction", "valuation_rate", "voucher_type", "voucher_no",
    )
    list_filter = ("company", "voucher_type", "posting_date", "is_cancelled")
    search_fields = ("name", "item__name", "warehouse__name", "voucher_no")
    readonly_fields = tuple(field.name for field in StockLedgerEntry._meta.fields)
    date_hierarchy = "posting_date"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
