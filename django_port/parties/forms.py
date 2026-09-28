from django import forms

from geo.models import Country

from .models import Customer, Supplier


class QuickEntryMixin(forms.Form):
    new_contact_email = forms.EmailField(required=False, help_text="Creates a primary contact if one is not set.")
    new_contact_mobile = forms.CharField(max_length=140, required=False)
    new_address_line1 = forms.CharField(max_length=240, required=False, help_text="Creates a primary address if one is not set.")
    new_address_line2 = forms.CharField(max_length=240, required=False)
    new_address_city = forms.CharField(max_length=140, required=False)
    new_address_state = forms.CharField(max_length=140, required=False)
    new_address_pincode = forms.CharField(max_length=140, required=False)
    new_address_country = forms.ModelChoiceField(queryset=Country.objects.all(), required=False)

    def clean(self):
        data = super().clean()
        if data.get("new_address_line1"):
            if not data.get("new_address_city"):
                self.add_error("new_address_city", "City is required for a new address.")
            if not data.get("new_address_country"):
                self.add_error("new_address_country", "Country is required for a new address.")
        elif any(data.get(field) for field in (
            "new_address_line2", "new_address_city", "new_address_state",
            "new_address_pincode", "new_address_country",
        )):
            self.add_error("new_address_line1", "Address line 1 is required for a new address.")
        return data


class CustomerQuickEntryForm(QuickEntryMixin, forms.ModelForm):
    new_contact_first_name = forms.CharField(max_length=140, required=False)
    new_contact_last_name = forms.CharField(max_length=140, required=False)

    class Meta:
        model = Customer
        fields = "__all__"


class SupplierQuickEntryForm(QuickEntryMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = "__all__"
