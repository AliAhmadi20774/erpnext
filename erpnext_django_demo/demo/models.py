from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models


class Customer(models.Model):
    name = models.CharField("نام مشتری", max_length=160)
    code = models.CharField("کد", max_length=30, unique=True)
    contact = models.CharField("نام تماس", max_length=100, blank=True)
    phone = models.CharField("تلفن", max_length=30, blank=True)
    city = models.CharField("شهر", max_length=80, blank=True)

    def __str__(self):
        return self.name


class Supplier(models.Model):
    name = models.CharField("نام تامین‌کننده", max_length=160)
    code = models.CharField("کد", max_length=30, unique=True)
    contact = models.CharField("نام تماس", max_length=100, blank=True)
    phone = models.CharField("تلفن", max_length=30, blank=True)
    city = models.CharField("شهر", max_length=80, blank=True)

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

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def needs_reorder(self):
        return self.stock <= self.reorder_level


class Order(models.Model):
    SALES = "sales"
    PURCHASE = "purchase"
    TYPES = [(SALES, "فروش"), (PURCHASE, "خرید")]
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    STATUSES = [(DRAFT, "پیش‌نویس"), (CONFIRMED, "تایید شده")]

    kind = models.CharField("نوع", max_length=8, choices=TYPES)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True, related_name="orders")
    status = models.CharField("وضعیت", max_length=10, choices=STATUSES, default=DRAFT)
    created_at = models.DateTimeField("تاریخ ایجاد", auto_now_add=True)
    confirmed_at = models.DateTimeField("تاریخ تایید", null=True, blank=True)
    notes = models.TextField("یادداشت", blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def number(self):
        return f"{'SO' if self.kind == self.SALES else 'PO'}-{self.created_at.year}-{self.pk:04d}"

    @property
    def party(self):
        return self.customer if self.kind == self.SALES else self.supplier

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


class StockMovement(models.Model):
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="movements")
    change = models.IntegerField("تغییر موجودی")
    created_at = models.DateTimeField("زمان", auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

