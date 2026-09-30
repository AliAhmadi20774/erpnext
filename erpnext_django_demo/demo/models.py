from decimal import Decimal
import uuid

import jdatetime
from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Customer(models.Model):
    name = models.CharField("نام مشتری", max_length=160)
    code = models.CharField("کد", max_length=30, unique=True)
    contact = models.CharField("نام تماس", max_length=100, blank=True)
    phone = models.CharField("تلفن", max_length=30, blank=True)
    city = models.CharField("شهر", max_length=80, blank=True)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("code"), name="demo_customer_code_ci")]

    def __str__(self):
        return self.name


class Supplier(models.Model):
    name = models.CharField("نام تامین‌کننده", max_length=160)
    code = models.CharField("کد", max_length=30, unique=True)
    contact = models.CharField("نام تماس", max_length=100, blank=True)
    phone = models.CharField("تلفن", max_length=30, blank=True)
    city = models.CharField("شهر", max_length=80, blank=True)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("code"), name="demo_supplier_code_ci")]

    def __str__(self):
        return self.name


class Item(models.Model):
    sku = models.CharField("کد کالا", max_length=40, unique=True)
    name = models.CharField("نام کالا", max_length=160)
    category = models.CharField("دسته‌بندی", max_length=80)
    unit = models.CharField("واحد", max_length=30, default="عدد")
    sale_price = models.DecimalField("قیمت فروش", max_digits=14, decimal_places=0, validators=[MinValueValidator(0)])
    purchase_price = models.DecimalField("قیمت خرید", max_digits=14, decimal_places=0, validators=[MinValueValidator(0)])
    stock = models.PositiveIntegerField("موجودی", default=0)
    reorder_level = models.PositiveIntegerField("حد سفارش", default=10)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("sku"), name="demo_item_sku_ci")]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def needs_reorder(self):
        return self.is_active and self.stock <= self.reorder_level


class Order(models.Model):
    SALES = "sales"
    PURCHASE = "purchase"
    TYPES = [(SALES, "فروش"), (PURCHASE, "خرید")]
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    STATUSES = [(DRAFT, "پیش‌نویس"), (CONFIRMED, "تایید شده"), (CANCELLED, "لغو شده")]

    kind = models.CharField("نوع", max_length=8, choices=TYPES)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    status = models.CharField("وضعیت", max_length=10, choices=STATUSES, default=DRAFT)
    created_at = models.DateTimeField("تاریخ ایجاد", auto_now_add=True)
    confirmed_at = models.DateTimeField("تاریخ تایید", null=True, blank=True)
    cancelled_at = models.DateTimeField("تاریخ لغو", null=True, blank=True)
    notes = models.TextField("یادداشت", blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def number(self):
        created_date = timezone.localtime(self.created_at).date()
        jalali_year = jdatetime.date.fromgregorian(date=created_date).year
        return f"{'SO' if self.kind == self.SALES else 'PO'}-{jalali_year}-{self.pk:04d}"

    @property
    def party(self):
        return self.customer if self.kind == self.SALES else self.supplier

    @property
    def workflow_label(self):
        if self.status == self.CANCELLED:
            return "لغو شده"
        if self.status == self.DRAFT:
            return "پیش‌نویس"
        if hasattr(self, "invoice"):
            if self.invoice.balance == 0:
                return "تسویه شده"
            if self.invoice.paid:
                return "پرداخت ناقص" if self.kind == self.PURCHASE else "دریافت ناقص"
            return "صورتحساب صادر شده"
        if hasattr(self, "fulfillment"):
            return "دریافت شده" if self.kind == self.PURCHASE else "تحویل شده"
        return "تایید شده"

    @property
    def total(self):
        return sum((line.total for line in self.lines.all()), Decimal("0"))

    def __str__(self):
        return self.number if self.pk else self.get_kind_display()


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField("تعداد", validators=[MinValueValidator(1)])
    unit_price = models.DecimalField("قیمت واحد", max_digits=14, decimal_places=0, validators=[MinValueValidator(0)])

    @property
    def total(self):
        return self.quantity * self.unit_price


class Fulfillment(models.Model):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="fulfillment")
    completed_at = models.DateTimeField("زمان تحویل یا دریافت", default=timezone.now)

    def __str__(self):
        return f"{'DN' if self.order.kind == Order.SALES else 'PR'}-{self.pk:04d}"


class Invoice(models.Model):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="invoice")
    amount = models.DecimalField("مبلغ", max_digits=16, decimal_places=0, validators=[MinValueValidator(1)])
    issued_at = models.DateTimeField("زمان صدور", default=timezone.now)

    @property
    def number(self):
        return f"{'SI' if self.order.kind == Order.SALES else 'PI'}-{self.pk:04d}"

    @property
    def paid(self):
        cached = getattr(self, "_prefetched_objects_cache", {}).get("payments")
        if cached is not None:
            return sum((payment.amount for payment in cached), Decimal("0"))
        return self.payments.aggregate(total=models.Sum("amount"))["total"] or Decimal("0")

    @property
    def balance(self):
        return self.amount - self.paid

    def __str__(self):
        return self.number


class Payment(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField("مبلغ", max_digits=16, decimal_places=0, validators=[MinValueValidator(1)])
    reference = models.CharField("شماره پیگیری", max_length=100, blank=True)
    paid_at = models.DateTimeField("زمان پرداخت", default=timezone.now)
    idempotency_key = models.UUIDField("شناسهٔ درخواست", default=uuid.uuid4, unique=True, editable=False)

    class Meta:
        ordering = ["paid_at", "pk"]


class StockMovement(models.Model):
    OPENING = "opening"
    SALES = "sales"
    PURCHASE = "purchase"
    ADJUSTMENT = "adjustment"
    SOURCES = [(OPENING, "موجودی افتتاحیه"), (SALES, "تحویل فروش"),
               (PURCHASE, "دریافت خرید"), (ADJUSTMENT, "اصلاح موجودی")]

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="movements", null=True, blank=True)
    source = models.CharField("نوع گردش", max_length=12, choices=SOURCES, default=ADJUSTMENT)
    change = models.IntegerField("تغییر موجودی")
    balance_before = models.IntegerField("ماندهٔ قبل", default=0)
    balance_after = models.IntegerField("ماندهٔ بعد", default=0)
    note = models.CharField("دلیل یا توضیح", max_length=255, blank=True)
    created_at = models.DateTimeField("زمان", default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="erp_audit_events", verbose_name="کاربر")
    action = models.CharField("رویداد", max_length=50)
    object_type = models.CharField("نوع سند", max_length=50)
    object_id = models.CharField("شناسهٔ سند", max_length=50)
    object_label = models.CharField("عنوان سند", max_length=160)
    details = models.JSONField("جزئیات", default=dict, blank=True)
    created_at = models.DateTimeField("زمان", default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.action}: {self.object_label}"

