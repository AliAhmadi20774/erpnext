from django.contrib import admin
from .models import (Account, AuditEvent, BillOfMaterials, BOMComponent, Customer, FitGapItem,
                     Item, JournalEntry, JournalLine, ManagementDecision, Order, OrderLine,
                     ProductionPlan, ProductionPlanLine, StockMovement, Supplier, WorkOrder,
                     WorkOrderMaterial)


@admin.register(Customer, Supplier)
class PartyAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "city", "is_active")
    list_filter = ("is_active", "city")
    search_fields = ("name", "code", "contact")


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "category", "stock", "reorder_level", "is_active")
    list_filter = ("is_active", "category")
    search_fields = ("name", "sku")
    readonly_fields = ("stock",)


admin.site.register([Order, OrderLine, StockMovement])


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "bom", "quantity", "status", "planned_start", "due_date")
    list_filter = ("status",)
    search_fields = ("bom__code", "bom__product__name", "bom__product__sku")
    readonly_fields = tuple(field.name for field in WorkOrder._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(WorkOrderMaterial)


@admin.register(ProductionPlan)
class ProductionPlanAdmin(admin.ModelAdmin):
    list_display = ("number", "product", "demand_quantity", "due_date", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("product__name", "product__sku", "bom__code")
    readonly_fields = tuple(field.name for field in ProductionPlan._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProductionPlanLine)
class ProductionPlanLineAdmin(admin.ModelAdmin):
    list_display = ("plan", "sequence", "item", "gross_requirement", "net_requirement",
                    "supply_type")
    list_filter = ("supply_type", "plan__status")
    search_fields = ("plan__id", "item__name", "item__sku", "supply_bom__code")
    readonly_fields = tuple(field.name for field in ProductionPlanLine._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "account_type", "is_active")
    list_filter = ("account_type", "is_active")
    search_fields = ("code", "name")
    readonly_fields = ("code", "name", "account_type", "is_active")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("number", "posted_at", "source_type", "source_label", "posted_by")
    list_filter = ("source_type",)
    search_fields = ("source_label", "description")
    readonly_fields = tuple(field.name for field in JournalEntry._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(JournalLine)
class JournalLineAdmin(admin.ModelAdmin):
    list_display = ("entry", "account", "debit", "credit", "memo")
    list_filter = ("account",)
    readonly_fields = tuple(field.name for field in JournalLine._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "object_type", "object_label")
    list_filter = ("action", "object_type")
    search_fields = ("object_label", "object_id", "actor__username")
    readonly_fields = ("actor", "action", "object_type", "object_id", "object_label", "details", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ManagementDecision)
class ManagementDecisionAdmin(admin.ModelAdmin):
    list_display = ("meeting_date", "outcome", "architecture", "owner", "due_date", "updated_at")
    list_filter = ("outcome", "architecture")
    search_fields = ("attendees", "concerns", "owner", "next_step")
    readonly_fields = tuple(field.name for field in ManagementDecision._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FitGapItem)
class FitGapItemAdmin(admin.ModelAdmin):
    list_display = ("title", "area", "fit", "priority", "risk", "status", "updated_at")
    list_filter = ("area", "fit", "priority", "risk", "phase", "status")
    search_fields = ("title", "requirement", "solution", "owner")
    readonly_fields = tuple(field.name for field in FitGapItem._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillOfMaterials)
class BillOfMaterialsAdmin(admin.ModelAdmin):
    list_display = ("code", "product", "version", "output_quantity", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("code", "product__name", "product__sku")
    readonly_fields = tuple(field.name for field in BillOfMaterials._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BOMComponent)
class BOMComponentAdmin(admin.ModelAdmin):
    list_display = ("bom", "item", "quantity", "scrap_percent", "sequence")
    search_fields = ("bom__code", "item__name", "item__sku")
    readonly_fields = tuple(field.name for field in BOMComponent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
