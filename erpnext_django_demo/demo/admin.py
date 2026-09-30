from django.contrib import admin
from .models import Customer, Item, Order, OrderLine, StockMovement, Supplier


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
