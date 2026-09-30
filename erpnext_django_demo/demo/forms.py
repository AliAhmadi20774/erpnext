from django import forms
from django.forms import formset_factory
from django.db.models import Q

from .models import Customer, Item, Supplier


class StyledFormMixin:
    def style_fields(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-control")


class UniqueCodeMixin:
    code_field = "code"

    def clean_code(self):
        value = self.cleaned_data["code"].strip().upper()
        if self._meta.model.objects.filter(code__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("این کد قبلا ثبت شده است.")
        return value


class CustomerForm(UniqueCodeMixin, StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["name", "code", "contact", "phone", "city"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class SupplierForm(UniqueCodeMixin, StyledFormMixin, forms.ModelForm):
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

    def clean_sku(self):
        value = self.cleaned_data["sku"].strip().upper()
        if Item.objects.filter(sku__iexact=value).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("این کد کالا قبلا ثبت شده است.")
        return value


class ItemEditForm(ItemForm):
    class Meta(ItemForm.Meta):
        fields = ["name", "sku", "category", "unit", "sale_price", "purchase_price", "reorder_level"]


class OrderForm(StyledFormMixin, forms.Form):
    party = forms.ModelChoiceField(queryset=Customer.objects.none(), label="طرف حساب")
    notes = forms.CharField(label="یادداشت", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, kind="sales", current_party=None, **kwargs):
        super().__init__(*args, **kwargs)
        model = Customer if kind == "sales" else Supplier
        active = Q(is_active=True)
        if current_party:
            active |= Q(pk=current_party.pk)
        self.fields["party"].queryset = model.objects.filter(active)
        self.fields["party"].label = "مشتری" if kind == "sales" else "تامین‌کننده"
        self.style_fields()


class OrderLineForm(StyledFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=Item.objects.filter(is_active=True), label="کالا", required=False)
    quantity = forms.IntegerField(label="تعداد", min_value=1, required=False)

    def __init__(self, *args, existing_item_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["item"].queryset = Item.objects.filter(Q(is_active=True) | Q(pk__in=existing_item_ids))
        self.style_fields()

    def clean(self):
        data = super().clean()
        if data.get("item") and data.get("quantity") is None:
            self.add_error("quantity", "تعداد را وارد کنید.")
        if data.get("quantity") is not None and not data.get("item"):
            self.add_error("item", "کالا را انتخاب کنید.")
        return data


OrderLineFormSet = formset_factory(OrderLineForm, extra=3, max_num=20, validate_max=True)


class PaymentForm(StyledFormMixin, forms.Form):
    amount = forms.DecimalField(label="مبلغ", min_value=1, max_digits=16, decimal_places=0)
    reference = forms.CharField(label="شماره پیگیری", max_length=100, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class StockAdjustmentForm(StyledFormMixin, forms.Form):
    new_stock = forms.IntegerField(label="موجودی جدید", min_value=0)
    reason = forms.CharField(label="دلیل اصلاح", max_length=255,
                             widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()

