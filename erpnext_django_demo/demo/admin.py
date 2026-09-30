from django.contrib import admin
from .models import (AuditEvent, BillOfMaterials, BOMComponent, Customer, FitGapItem, Item,
                     ManagementDecision, Order, OrderLine, StockMovement, Supplier)


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
