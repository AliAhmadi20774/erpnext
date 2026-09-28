from django.contrib import admin

from .models import Address, AddressPartyLink, Contact, ContactEmail, ContactPartyLink, ContactPhone


class AddressPartyLinkInline(admin.TabularInline):
    model = AddressPartyLink
    extra = 0


class ContactPartyLinkInline(admin.TabularInline):
    model = ContactPartyLink
    extra = 0


class ContactEmailInline(admin.TabularInline):
    model = ContactEmail
    extra = 0


class ContactPhoneInline(admin.TabularInline):
    model = ContactPhone
    extra = 0


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ("name", "address_title", "address_type", "city", "country", "disabled")
    list_filter = ("address_type", "country", "disabled", "is_primary_address", "is_shipping_address")
    search_fields = ("name", "address_title", "city", "pincode")
    inlines = (AddressPartyLinkInline,)

    def get_readonly_fields(self, request, obj=None):
        return ["name"] if obj else []

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj else []


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "first_name", "last_name", "email_id", "phone", "mobile_no", "is_primary_contact")
    list_filter = ("status", "is_primary_contact", "unsubscribed")
    search_fields = ("name", "first_name", "last_name", "email_ids__email_id")
    inlines = (ContactEmailInline, ContactPhoneInline, ContactPartyLinkInline)

    def get_readonly_fields(self, request, obj=None):
        return ["name"] if obj else []

    def get_inline_instances(self, request, obj=None):
        return super().get_inline_instances(request, obj) if obj else []
