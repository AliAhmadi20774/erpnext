from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from .entries import submit_stock_entry
from .models import (
    Bin,
    StockEntry,
    StockEntryDetail,
    StockEntryType,
    StockLedgerEntry,
    Warehouse,
    WarehouseType,
)


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


@admin.register(StockEntryType)
class StockEntryTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "purpose", "add_to_transit", "is_standard")
    list_filter = ("purpose", "add_to_transit", "is_standard")
    search_fields = ("name",)
    readonly_fields = ("is_standard", "batch_split")


class StockEntryDetailInline(admin.TabularInline):
    model = StockEntryDetail
    extra = 1
    fields = (
        "position", "item", "source_warehouse", "target_warehouse", "qty", "uom",
        "conversion_factor", "basic_rate", "allow_zero_valuation_rate", "project",
    )


@admin.register(StockEntry)
class StockEntryAdmin(admin.ModelAdmin):
    list_display = (
        "name", "posting_date", "company", "stock_entry_type", "purpose",
        "total_incoming_value", "total_outgoing_value", "status",
    )
    list_filter = ("company", "stock_entry_type", "status", "posting_date")
    search_fields = ("name", "remarks")
    readonly_fields = (
        "name", "purpose", "total_incoming_value", "total_outgoing_value",
        "value_difference", "total_amount", "status",
    )
    inlines = (StockEntryDetailInline,)
    actions = ("submit_selected",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or obj.status == StockEntry.Status.DRAFT
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.status == StockEntry.Status.DRAFT
        )

    def delete_queryset(self, request, queryset):
        for stock_entry in queryset:
            stock_entry.delete()

    @admin.action(description="Submit selected stock entries")
    def submit_selected(self, request, queryset):
        for stock_entry in queryset.order_by("company", "posting_date", "posting_time", "name"):
            try:
                submit_stock_entry(stock_entry)
            except ValidationError as error:
                self.message_user(request, f"{stock_entry.name}: {error}", level=messages.ERROR)
            else:
                self.message_user(
                    request, f"Submitted {stock_entry.name}.", level=messages.SUCCESS
                )
