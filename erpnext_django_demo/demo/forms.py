from django import forms
from django.forms import formset_factory

from .models import Customer, Item, Supplier


class StyledFormMixin:
    def style_fields(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")


class CustomerForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "code", "contact", "phone", "city"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class SupplierForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "code", "contact", "phone", "city"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class ItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Item
        fields = ["name", "sku", "category", "unit", "sale_price", "purchase_price", "stock", "reorder_level"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class OrderForm(StyledFormMixin, forms.Form):
    party = forms.ModelChoiceField(queryset=Customer.objects.none(), label="طرف حساب")
    notes = forms.CharField(label="یادداشت", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, kind="sales", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["party"].queryset = Customer.objects.all() if kind == "sales" else Supplier.objects.all()
        self.fields["party"].label = "مشتری" if kind == "sales" else "تامین‌کننده"
        self.style_fields()


class OrderLineForm(StyledFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=Item.objects.all(), label="کالا", required=False)
    quantity = forms.IntegerField(label="تعداد", min_value=1, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean(self):
        data = super().clean()
        if data.get("item") and data.get("quantity") is None:
            self.add_error("quantity", "تعداد را وارد کنید.")
        if data.get("quantity") is not None and not data.get("item"):
            self.add_error("item", "کالا را انتخاب کنید.")
        return data


OrderLineFormSet = formset_factory(OrderLineForm, extra=3, max_num=20, validate_max=True)

