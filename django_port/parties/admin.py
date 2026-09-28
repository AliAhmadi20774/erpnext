from django.contrib import admin
from django.db import transaction
from contacts.models import AddressPartyLink, ContactPartyLink
from contacts.services import create_primary_address, create_primary_contact
from accounting.models import PartyAccount

from .forms import CustomerQuickEntryForm, SupplierQuickEntryForm
from .models import Customer, CustomerGroup, Supplier, SupplierGroup, Territory


class TreeAdmin(admin.ModelAdmin):
    list_display = ("name", "parent_name", "is_group", "lft", "rgt")
    list_filter = ("is_group",)
    search_fields = ("name",)
    readonly_fields = ("lft", "rgt")

    @admin.display(description="Parent")
    def parent_name(self, obj):
        return getattr(obj, obj.parent_field)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["name"] if obj else [])

    def delete_queryset(self, request, queryset):
        for node in queryset.order_by("-lft"):
            node.delete()


class CustomerGroupAccountInline(admin.TabularInline):
    model = PartyAccount
    fk_name = "customer_group"
    exclude = ("customer", "supplier", "supplier_group")
    extra = 0


class SupplierGroupAccountInline(admin.TabularInline):
    model = PartyAccount
    fk_name = "supplier_group"
    exclude = ("customer", "supplier", "customer_group")
    extra = 0


@admin.register(CustomerGroup)
class CustomerGroupAdmin(TreeAdmin):
    inlines = (CustomerGroupAccountInline,)


@admin.register(SupplierGroup)
class SupplierGroupAdmin(TreeAdmin):
    inlines = (SupplierGroupAccountInline,)


admin.site.register(Territory, TreeAdmin)


class CustomerAddressLinkInline(admin.TabularInline):
    model = AddressPartyLink
    fk_name = "customer"
    exclude = ("supplier",)
    extra = 0


class CustomerContactLinkInline(admin.TabularInline):
    model = ContactPartyLink
    fk_name = "customer"
    exclude = ("supplier",)
    extra = 0


class SupplierAddressLinkInline(admin.TabularInline):
    model = AddressPartyLink
    fk_name = "supplier"
    exclude = ("customer",)
    extra = 0


class SupplierContactLinkInline(admin.TabularInline):
    model = ContactPartyLink
    fk_name = "supplier"
    exclude = ("customer",)
    extra = 0


class CustomerAccountInline(admin.TabularInline):
    model = PartyAccount
    fk_name = "customer"
    exclude = ("supplier", "customer_group", "supplier_group")
    extra = 0


class SupplierAccountInline(admin.TabularInline):
    model = PartyAccount
    fk_name = "supplier"
    exclude = ("customer", "customer_group", "supplier_group")
    extra = 0


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    form = CustomerQuickEntryForm
    list_display = ("name", "customer_name", "customer_group", "territory", "email_id", "mobile_no", "disabled")
    list_filter = ("disabled", "customer_type", "customer_group", "territory")
    search_fields = ("name", "customer_name", "tax_id", "customer_primary_contact__email_ids__email_id")
    readonly_fields = ("email_id", "mobile_no", "first_name", "last_name", "primary_address")
    inlines = (CustomerAddressLinkInline, CustomerContactLinkInline, CustomerAccountInline)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["name"] if obj else [])

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj else []

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _create_quick_entry_records(obj, form.cleaned_data)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    form = SupplierQuickEntryForm
    list_display = ("name", "supplier_name", "supplier_group", "country", "email_id", "mobile_no", "disabled")
    list_filter = ("disabled", "supplier_type", "supplier_group", "country")
    search_fields = ("name", "supplier_name", "tax_id", "supplier_primary_contact__email_ids__email_id")
    readonly_fields = ("email_id", "mobile_no", "primary_address")
    inlines = (SupplierAddressLinkInline, SupplierContactLinkInline, SupplierAccountInline)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["name"] if obj else [])

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj else []

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _create_quick_entry_records(obj, form.cleaned_data)


def _create_quick_entry_records(party, data):
    if data.get("new_address_line1"):
        create_primary_address(
            party,
            address_line1=data["new_address_line1"],
            address_line2=data.get("new_address_line2", ""),
            city=data["new_address_city"],
            state=data.get("new_address_state", ""),
            pincode=data.get("new_address_pincode", ""),
            country=data["new_address_country"],
        )
    if any(data.get(field) for field in (
        "new_contact_email", "new_contact_mobile", "new_contact_first_name", "new_contact_last_name",
    )):
        create_primary_contact(
            party,
            email_id=data.get("new_contact_email", ""),
            mobile_no=data.get("new_contact_mobile", ""),
            first_name=data.get("new_contact_first_name", ""),
            last_name=data.get("new_contact_last_name", ""),
        )
