from django import forms
from django.forms import formset_factory
from django.db.models import Q
import uuid

from .models import (BillOfMaterials, Customer, FitGapItem, Item, ManagementDecision, Supplier,
                     WorkOrder)


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
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

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


class ManagementDecisionForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = ManagementDecision
        fields = ["meeting_date", "attendees", "outcome", "architecture", "positives", "concerns",
                  "gap_summary", "next_step", "owner", "due_date", "budget_ceiling"]
        widgets = {
            "meeting_date": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "attendees": forms.Textarea(attrs={"rows": 2}),
            "positives": forms.Textarea(attrs={"rows": 3}),
            "concerns": forms.Textarea(attrs={"rows": 3}),
            "gap_summary": forms.Textarea(attrs={"rows": 3}),
            "next_step": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean(self):
        data = super().clean()
        if data.get("outcome") != ManagementDecision.PENDING:
            required = {
                "meeting_date": "تاریخ جلسه را وارد کنید.",
                "attendees": "حاضران جلسه را ثبت کنید.",
                "positives": "حداقل یک نکتهٔ مثبت را ثبت کنید.",
                "concerns": "نگرانی‌ها یا شکاف‌های اصلی را ثبت کنید.",
                "gap_summary": "فاصلهٔ نیاز واقعی با نمونه را جمع‌بندی کنید.",
                "next_step": "اقدام بعدی را مشخص کنید.",
                "owner": "مالک اقدام بعدی را مشخص کنید.",
                "due_date": "موعد پیگیری را وارد کنید.",
            }
            for field, message in required.items():
                value = data.get(field)
                if value is None or isinstance(value, str) and not value.strip():
                    self.add_error(field, message)
            if data.get("architecture") == ManagementDecision.UNDECIDED:
                self.add_error("architecture", "گزینهٔ معماری مورد توافق را مشخص کنید.")
            if data.get("meeting_date") and data.get("due_date") and data["due_date"] < data["meeting_date"]:
                self.add_error("due_date", "موعد پیگیری نمی‌تواند پیش از تاریخ جلسه باشد.")
        return data


class FitGapItemForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = FitGapItem
        fields = ["area", "title", "requirement", "current_process", "evidence", "fit",
                  "solution", "acceptance_criteria", "priority", "effort", "risk", "phase",
                  "cost_low", "cost_high", "owner", "status"]
        widgets = {
            "requirement": forms.Textarea(attrs={"rows": 3}),
            "current_process": forms.Textarea(attrs={"rows": 3}),
            "evidence": forms.Textarea(attrs={"rows": 3}),
            "solution": forms.Textarea(attrs={"rows": 3}),
            "acceptance_criteria": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()

    def clean(self):
        data = super().clean()
        low, high = data.get("cost_low"), data.get("cost_high")
        if low is not None and high is not None and high < low:
            self.add_error("cost_high", "حداکثر هزینه نمی‌تواند از حداقل کمتر باشد.")
        if data.get("status") != FitGapItem.DRAFT:
            required = {
                "current_process": "وضعیت فعلی را ثبت کنید.",
                "evidence": "منبع یا شاهد نیاز را ثبت کنید.",
                "solution": "راهکار پیشنهادی را ثبت کنید.",
                "acceptance_criteria": "معیار پذیرش را ثبت کنید.",
                "owner": "مالک بررسی یا اجرا را مشخص کنید.",
                "cost_low": "حداقل هزینه را برآورد کنید.",
                "cost_high": "حداکثر هزینه را برآورد کنید.",
            }
            for field, message in required.items():
                value = data.get(field)
                if value is None or isinstance(value, str) and not value.strip():
                    self.add_error(field, message)
            if data.get("fit") == FitGapItem.UNKNOWN:
                self.add_error("fit", "وضعیت انطباق را تعیین کنید.")
            if data.get("effort") == FitGapItem.UNKNOWN:
                self.add_error("effort", "تلاش لازم را برآورد کنید.")
        return data


class BOMCreateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BillOfMaterials
        fields = ["product", "output_quantity", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Item.objects.filter(is_active=True).order_by("name")
        self.style_fields()


class BOMDraftForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = BillOfMaterials
        fields = ["output_quantity", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.style_fields()


class BOMComponentForm(StyledFormMixin, forms.Form):
    item = forms.ModelChoiceField(queryset=Item.objects.none(), label="جزء", required=False)
    quantity = forms.DecimalField(label="مقدار مصرف", min_value=0.001, max_digits=12,
                                  decimal_places=3, required=False)
    scrap_percent = forms.DecimalField(label="ضایعات ٪", min_value=0, max_value=100,
                                       max_digits=5, decimal_places=2, initial=0, required=False)
    notes = forms.CharField(label="توضیح", max_length=255, required=False)

    def __init__(self, *args, product=None, existing_item_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.product = product
        self.fields["item"].queryset = Item.objects.filter(
            Q(is_active=True) | Q(pk__in=existing_item_ids)).order_by("name")
        self.style_fields()

    def clean(self):
        data = super().clean()
        item, quantity = data.get("item"), data.get("quantity")
        if item and quantity is None:
            self.add_error("quantity", "مقدار مصرف را وارد کنید.")
        if quantity is not None and not item:
            self.add_error("item", "جزء را انتخاب کنید.")
        if item and self.product and item.pk == self.product.pk:
            self.add_error("item", "محصول نمی‌تواند جزء مستقیم BOM خودش باشد.")
        data["scrap_percent"] = data.get("scrap_percent") or 0
        return data


BOMComponentFormSet = formset_factory(BOMComponentForm, extra=3, max_num=50, validate_max=True)


class WorkOrderForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ["bom", "quantity", "planned_start", "due_date", "notes"]
        widgets = {
            "planned_start": forms.DateInput(attrs={"type": "date"}),
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bom"].queryset = BillOfMaterials.objects.filter(
            status=BillOfMaterials.ACTIVE).select_related("product").order_by("product__name")
        self.style_fields()

    def clean(self):
        data = super().clean()
        if data.get("planned_start") and data.get("due_date") \
                and data["due_date"] < data["planned_start"]:
            self.add_error("due_date", "موعد تکمیل نمی‌تواند پیش از تاریخ شروع باشد.")
        return data

