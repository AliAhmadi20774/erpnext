from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet

from .models import (
    Item,
    ItemGroup,
    ItemPrice,
    ItemUOMConversion,
    PriceList,
    PriceListCountry,
    UOMCategory,
    UOMConversionFactor,
    UnitOfMeasure,
)


@admin.register(UOMCategory)
class UOMCategoryAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(UnitOfMeasure)
class UnitOfMeasureAdmin(admin.ModelAdmin):
    list_display = ("name", "symbol", "category", "enabled", "must_be_whole_number")
    list_filter = ("enabled", "must_be_whole_number", "category")
    search_fields = ("name", "symbol", "common_code")


@admin.register(UOMConversionFactor)
class UOMConversionFactorAdmin(admin.ModelAdmin):
    list_display = ("from_uom", "to_uom", "value", "category")
    list_filter = ("category",)
    search_fields = ("from_uom__name", "to_uom__name")


@admin.register(ItemGroup)
class ItemGroupAdmin(admin.ModelAdmin):
    list_display = ("name", "parent_item_group", "is_group", "lft", "rgt")
    list_filter = ("is_group",)
    search_fields = ("name",)
    readonly_fields = ("lft", "rgt")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["name"] if obj else [])

    def delete_queryset(self, request, queryset):
        for group in queryset.order_by("-lft"):
            group.delete()


class ItemUOMConversionFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        for form in self.forms:
            if (
                form.cleaned_data
                and form.cleaned_data.get("DELETE")
                and form.instance.uom_id == self.instance.stock_uom_id
            ):
                raise ValidationError("The stock UOM conversion cannot be deleted.")


class ItemUOMConversionInline(admin.TabularInline):
    model = ItemUOMConversion
    extra = 0
    formset = ItemUOMConversionFormSet


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = ("name", "item_name", "item_group", "stock_uom", "disabled")
    list_filter = ("disabled", "is_stock_item", "is_purchase_item", "is_sales_item", "item_group")
    search_fields = ("name", "item_name")
    inlines = (ItemUOMConversionInline,)

    def get_readonly_fields(self, request, obj=None):
        return ["name"] if obj else []

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj else []


class PriceListCountryInline(admin.TabularInline):
    model = PriceListCountry
    extra = 0


@admin.register(PriceList)
class PriceListAdmin(admin.ModelAdmin):
    list_display = ("name", "currency", "enabled", "buying", "selling")
    list_filter = ("enabled", "buying", "selling", "currency")
    search_fields = ("name",)
    inlines = (PriceListCountryInline,)


@admin.register(ItemPrice)
class ItemPriceAdmin(admin.ModelAdmin):
    list_display = ("item", "price_list", "uom", "price_list_rate", "currency", "valid_from", "valid_upto")
    list_filter = ("price_list", "currency", "buying", "selling")
    search_fields = (
        "item__name", "item__item_name", "price_list__name", "customer__name",
        "supplier__name", "legacy_customer_name", "legacy_supplier_name",
    )
    readonly_fields = (
        "item_name", "item_description", "currency", "buying", "selling", "reference",
        "legacy_customer_name", "legacy_supplier_name",
    )
