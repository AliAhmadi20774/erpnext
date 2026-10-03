from decimal import Decimal
import uuid

import jdatetime
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
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
    lead_time_days = models.PositiveIntegerField("زمان تامین (روز کاری)", default=0)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("sku"), name="demo_item_sku_ci")]

    def __str__(self):
        return f"{self.name} ({self.sku})"

    @property
    def needs_reorder(self):
        return self.is_active and self.stock <= self.reorder_level


class BillOfMaterials(models.Model):
    DRAFT = "draft"
    ACTIVE = "active"
    OBSOLETE = "obsolete"
    STATUSES = [(DRAFT, "پیش‌نویس"), (ACTIVE, "فعال"), (OBSOLETE, "منسوخ")]

    product = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="boms",
                                verbose_name="محصول")
    code = models.CharField("کد BOM", max_length=50, unique=True)
    version = models.PositiveIntegerField("نسخه", default=1, validators=[MinValueValidator(1)])
    manufacturing_days = models.PositiveIntegerField("مدت ساخت (روز کاری)", default=1)
    output_quantity = models.DecimalField("مقدار خروجی", max_digits=12, decimal_places=3,
                                          default=1, validators=[MinValueValidator(0.001)])
    status = models.CharField("وضعیت", max_length=10, choices=STATUSES, default=DRAFT)
    notes = models.TextField("توضیحات مهندسی", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="created_boms",
                                   verbose_name="ایجادکننده")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="updated_boms",
                                   verbose_name="آخرین ویرایش‌کننده")
    activated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="activated_boms",
                                     verbose_name="فعال‌کننده")
    activated_at = models.DateTimeField("زمان فعال‌سازی", null=True, blank=True)
    created_at = models.DateTimeField("زمان ایجاد", auto_now_add=True)
    updated_at = models.DateTimeField("آخرین ویرایش", auto_now=True)

    class Meta:
        ordering = ["product__name", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["product", "version"], name="demo_bom_product_version"),
            models.UniqueConstraint(fields=["product"], condition=models.Q(status="active"),
                                    name="demo_one_active_bom_per_product"),
        ]

    def __str__(self):
        return f"{self.code} — {self.product.name}"


class BOMComponent(models.Model):
    bom = models.ForeignKey(BillOfMaterials, on_delete=models.CASCADE, related_name="components",
                            verbose_name="BOM")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="used_in_boms",
                             verbose_name="جزء")
    quantity = models.DecimalField("مقدار مصرف", max_digits=12, decimal_places=3,
                                   validators=[MinValueValidator(0.001)])
    scrap_percent = models.DecimalField("درصد ضایعات", max_digits=5, decimal_places=2, default=0,
                                        validators=[MinValueValidator(0), MaxValueValidator(100)])
    sequence = models.PositiveIntegerField("ترتیب", default=10)
    notes = models.CharField("توضیح", max_length=255, blank=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [models.UniqueConstraint(fields=["bom", "item"],
                                              name="demo_unique_bom_component")]

    def clean(self):
        super().clean()
        if self.bom_id and self.item_id and self.bom.product_id == self.item_id:
            raise ValidationError("محصول نمی‌تواند جزء مستقیم BOM خودش باشد.")

    def __str__(self):
        return f"{self.bom.code}: {self.item.name} × {self.quantity}"


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
    due_date = models.DateField("موعد تحویل / دریافت", null=True, blank=True)
    payment_due_date = models.DateField("سررسید صورتحساب", null=True, blank=True)
    source_plan = models.ForeignKey("ProductionPlan", on_delete=models.PROTECT, null=True,
                                    blank=True, related_name="purchase_orders")
    source_plan_line = models.ForeignKey("ProductionPlanLine", on_delete=models.PROTECT,
                                         null=True, blank=True,
                                         related_name="purchase_orders")

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

    def __str__(self):
        return f"{self.order.number} — {self.item.name} × {self.quantity}"


class Fulfillment(models.Model):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="fulfillment")
    completed_at = models.DateTimeField("زمان تحویل یا دریافت", default=timezone.now)

    def __str__(self):
        return f"{'DN' if self.order.kind == Order.SALES else 'PR'}-{self.pk:04d}"


class Invoice(models.Model):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="invoice")
    amount = models.DecimalField("مبلغ", max_digits=16, decimal_places=0, validators=[MinValueValidator(1)])
    issued_at = models.DateTimeField("زمان صدور", default=timezone.now)
    due_date = models.DateField("سررسید", null=True, blank=True)

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


class WorkOrder(models.Model):
    DRAFT = "draft"
    RELEASED = "released"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STATUSES = [(DRAFT, "پیش‌نویس"), (RELEASED, "آزادشده برای تولید"),
                (COMPLETED, "تکمیل‌شده"), (CANCELLED, "لغوشده")]

    bom = models.ForeignKey(BillOfMaterials, on_delete=models.PROTECT, related_name="work_orders",
                            verbose_name="نسخهٔ BOM")
    quantity = models.PositiveIntegerField("تعداد برنامه‌ریزی‌شده", validators=[MinValueValidator(1)])
    status = models.CharField("وضعیت", max_length=12, choices=STATUSES, default=DRAFT)
    planned_start = models.DateField("شروع برنامه‌ریزی‌شده", default=timezone.localdate)
    due_date = models.DateField("موعد تکمیل")
    notes = models.TextField("یادداشت تولید", blank=True)
    source_plan = models.ForeignKey("ProductionPlan", on_delete=models.PROTECT, null=True,
                                    blank=True, related_name="work_orders")
    source_plan_line = models.ForeignKey("ProductionPlanLine", on_delete=models.PROTECT,
                                         null=True, blank=True,
                                         related_name="work_orders")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="created_work_orders")
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="released_work_orders")
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="completed_work_orders")
    created_at = models.DateTimeField("زمان ایجاد", auto_now_add=True)
    released_at = models.DateTimeField("زمان آزادسازی", null=True, blank=True)
    completed_at = models.DateTimeField("زمان تکمیل", null=True, blank=True)
    cancelled_at = models.DateTimeField("زمان لغو", null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def number(self):
        return f"WO-{self.pk:05d}" if self.pk else "WO"

    @property
    def product(self):
        return self.bom.product

    def __str__(self):
        return f"{self.number} — {self.product.name}"


class WorkOrderMaterial(models.Model):
    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="materials")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="work_order_materials")
    required_quantity = models.PositiveIntegerField("مقدار مورد نیاز", validators=[MinValueValidator(1)])
    unit_cost = models.DecimalField("بهای واحد مبنا", max_digits=14, decimal_places=0,
                                    validators=[MinValueValidator(0)])
    sequence = models.PositiveIntegerField("ترتیب", default=10)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [models.UniqueConstraint(fields=["work_order", "item"],
                                               name="demo_unique_work_order_material")]

    @property
    def total_cost(self):
        return self.required_quantity * self.unit_cost

    def __str__(self):
        return f"{self.work_order.number}: {self.item.name} × {self.required_quantity}"


class ProductionPlan(models.Model):
    OPEN = "open"
    CLOSED = "closed"
    STATUSES = [(OPEN, "باز"), (CLOSED, "بسته‌شده")]

    product = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="production_plans")
    source_order_line = models.ForeignKey(OrderLine, on_delete=models.PROTECT, null=True,
                                         blank=True, related_name="production_plans",
                                         verbose_name="قلم سفارش مشتری")
    bom = models.ForeignKey(BillOfMaterials, on_delete=models.PROTECT,
                            related_name="production_plans")
    demand_quantity = models.PositiveIntegerField("تقاضای برنامه", validators=[MinValueValidator(1)])
    due_date = models.DateField("تاریخ نیاز")
    status = models.CharField("وضعیت", max_length=10, choices=STATUSES, default=OPEN)
    notes = models.TextField("یادداشت برنامه‌ریزی", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="created_production_plans")
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="closed_production_plans")
    created_at = models.DateTimeField("زمان اجرا", auto_now_add=True)
    closed_at = models.DateTimeField("زمان بستن", null=True, blank=True)
    schedule_snapshot = models.JSONField("مبنای زمان‌بندی", default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def number(self):
        return f"MRP-{self.pk:05d}" if self.pk else "MRP"

    def __str__(self):
        return f"{self.number} — {self.product.name}"


class ProductionPlanLine(models.Model):
    MAKE = "make"
    BUY = "buy"
    COVERED = "covered"
    SUPPLY_TYPES = [(MAKE, "ساخت"), (BUY, "خرید"), (COVERED, "تامین‌شده")]

    plan = models.ForeignKey(ProductionPlan, on_delete=models.CASCADE, related_name="lines")
    parent = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="children")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="production_plan_lines")
    supply_bom = models.ForeignKey(BillOfMaterials, on_delete=models.PROTECT, null=True,
                                   blank=True, related_name="production_plan_lines")
    level = models.PositiveIntegerField("سطح")
    sequence = models.PositiveIntegerField("ترتیب")
    gross_requirement = models.PositiveIntegerField("نیاز ناخالص")
    allocated_stock = models.PositiveIntegerField("تامین از موجودی", default=0)
    scheduled_receipts = models.PositiveIntegerField("دریافت برنامه‌ریزی‌شده", default=0)
    net_requirement = models.PositiveIntegerField("نیاز خالص", default=0)
    supply_type = models.CharField("پیشنهاد تامین", max_length=10, choices=SUPPLY_TYPES)
    required_date = models.DateField("موعد نیاز", null=True, blank=True)
    release_date = models.DateField("زمان شروع / سفارش‌گذاری", null=True, blank=True)

    class Meta:
        ordering = ["sequence", "id"]

    def __str__(self):
        return f"{self.plan.number}: {self.item.name}"


def default_workdays():
    return [5, 6, 0, 1, 2]


class PlanningPolicy(models.Model):
    working_weekdays = models.JSONField("روزهای کاری هفته", default=default_workdays)
    holidays = models.JSONField("تعطیلات (تاریخ میلادی)", default=list, blank=True)

    def clean(self):
        super().clean()
        if (not isinstance(self.working_weekdays, list) or not self.working_weekdays
                or any(type(day) is not int or day not in range(7) for day in self.working_weekdays)):
            raise ValidationError("حداقل یک روز کاری معتبر لازم است.")
        from datetime import date
        try:
            for value in self.holidays:
                date.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValidationError("تعطیلات باید فهرست تاریخ‌های معتبر YYYY-MM-DD باشند.")


class Account(models.Model):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    INCOME = "income"
    EXPENSE = "expense"
    TYPES = [(ASSET, "دارایی"), (LIABILITY, "بدهی"), (EQUITY, "حقوق مالکانه"),
             (INCOME, "درآمد"), (EXPENSE, "هزینه")]

    code = models.CharField("کد حساب", max_length=20, unique=True)
    name = models.CharField("نام حساب", max_length=120)
    account_type = models.CharField("نوع حساب", max_length=12, choices=TYPES)
    is_active = models.BooleanField("فعال", default=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"


class JournalEntry(models.Model):
    source_type = models.CharField("نوع سند مبنا", max_length=30)
    source_id = models.PositiveBigIntegerField("شناسهٔ سند مبنا")
    source_label = models.CharField("شمارهٔ سند مبنا", max_length=80)
    description = models.CharField("شرح", max_length=255)
    posted_at = models.DateTimeField("زمان ثبت", default=timezone.now, db_index=True)
    posted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="posted_journal_entries",
                                  verbose_name="ثبت‌کننده")

    class Meta:
        ordering = ["-posted_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["source_type", "source_id"],
                                               name="demo_unique_accounting_source")]

    @property
    def number(self):
        return f"JV-{self.pk:05d}" if self.pk else "JV"

    @property
    def total(self):
        cached = getattr(self, "_prefetched_objects_cache", {}).get("lines")
        if cached is not None:
            return sum((line.debit for line in cached), Decimal("0"))
        return self.lines.aggregate(value=models.Sum("debit"))["value"] or Decimal("0")

    def __str__(self):
        return f"{self.number} — {self.source_label}"


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines",
                              verbose_name="سند حسابداری")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines",
                                verbose_name="حساب")
    debit = models.DecimalField("بدهکار", max_digits=18, decimal_places=0, default=0,
                                validators=[MinValueValidator(0)])
    credit = models.DecimalField("بستانکار", max_digits=18, decimal_places=0, default=0,
                                 validators=[MinValueValidator(0)])
    memo = models.CharField("شرح ردیف", max_length=180, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(
            condition=(models.Q(debit__gt=0, credit=0) | models.Q(credit__gt=0, debit=0)),
            name="demo_journal_line_one_side",
        )]


class StockMovement(models.Model):
    OPENING = "opening"
    SALES = "sales"
    PURCHASE = "purchase"
    ADJUSTMENT = "adjustment"
    MANUFACTURE_ISSUE = "manufacture_issue"
    MANUFACTURE_RECEIPT = "manufacture_receipt"
    SOURCES = [(OPENING, "موجودی افتتاحیه"), (SALES, "تحویل فروش"),
               (PURCHASE, "دریافت خرید"), (ADJUSTMENT, "اصلاح موجودی"),
               (MANUFACTURE_ISSUE, "مصرف در تولید"),
               (MANUFACTURE_RECEIPT, "رسید محصول تولیدی")]

    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="movements")
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="movements", null=True, blank=True)
    work_order = models.ForeignKey(WorkOrder, on_delete=models.PROTECT, related_name="movements",
                                   null=True, blank=True)
    source = models.CharField("نوع گردش", max_length=24, choices=SOURCES, default=ADJUSTMENT)
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


class ManagementDecision(models.Model):
    PENDING = "pending"
    STOP = "stop"
    DISCOVERY = "discovery"
    PILOT = "pilot"
    OUTCOMES = [
        (PENDING, "در انتظار تصمیم"),
        (STOP, "توقف"),
        (DISCOVERY, "فاز کشف"),
        (PILOT, "پایلوت محدود"),
    ]
    UNDECIDED = "undecided"
    ERPNEXT_STANDARD = "erpnext_standard"
    ERPNEXT_CUSTOM = "erpnext_custom"
    ERPNEXT_API_UI = "erpnext_api_ui"
    INDEPENDENT = "independent"
    ARCHITECTURES = [
        (UNDECIDED, "هنوز تعیین نشده"),
        (ERPNEXT_STANDARD, "ERPNext استاندارد"),
        (ERPNEXT_CUSTOM, "ERPNext با Custom App محدود"),
        (ERPNEXT_API_UI, "ERPNext با رابط اختصاصی از API"),
        (INDEPENDENT, "محصول مستقل"),
    ]

    meeting_date = models.DateField("تاریخ جلسه", null=True, blank=True)
    attendees = models.TextField("حاضران", blank=True)
    outcome = models.CharField("نتیجه", max_length=20, choices=OUTCOMES, default=PENDING)
    architecture = models.CharField("گزینهٔ معماری", max_length=30, choices=ARCHITECTURES,
                                    default=UNDECIDED)
    positives = models.TextField("نکات مثبت", blank=True)
    concerns = models.TextField("نگرانی‌ها و شکاف‌ها", blank=True)
    gap_summary = models.TextField("جمع‌بندی نیاز و فاصلهٔ واقعی", blank=True)
    next_step = models.TextField("اقدام بعدی", blank=True)
    owner = models.CharField("مالک اقدام", max_length=160, blank=True)
    due_date = models.DateField("موعد پیگیری", null=True, blank=True)
    budget_ceiling = models.DecimalField("سقف بودجه (تومان)", max_digits=18, decimal_places=0,
                                         null=True, blank=True, validators=[MinValueValidator(0)])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name="created_erp_decisions", verbose_name="ثبت‌کننده")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name="updated_erp_decisions", verbose_name="آخرین ویرایش‌کننده")
    created_at = models.DateTimeField("زمان ثبت", auto_now_add=True)
    updated_at = models.DateTimeField("آخرین ویرایش", auto_now=True)

    class Meta:
        ordering = ["-meeting_date", "-created_at"]

    @property
    def is_actionable(self):
        if self.outcome == self.PENDING:
            return False
        required = (self.meeting_date, self.attendees.strip(), self.positives.strip(),
                    self.concerns.strip(), self.gap_summary.strip(), self.next_step.strip(),
                    self.owner.strip(), self.due_date)
        return bool(all(required) and self.architecture != self.UNDECIDED)

    def __str__(self):
        return f"{self.get_outcome_display()} — {self.meeting_date or 'بدون تاریخ'}"


class FitGapItem(models.Model):
    AREAS = [
        ("accounting", "حسابداری و مالی"),
        ("tax", "مالیات و الزامات قانونی"),
        ("sales", "فروش و قیمت‌گذاری"),
        ("purchase", "خرید و تامین"),
        ("inventory", "انبار و لجستیک"),
        ("hr", "منابع انسانی"),
        ("multi_company", "چندشرکتی و شعب"),
        ("manufacturing", "تولید"),
        ("integration", "اتصال‌ها و تبادل داده"),
        ("reporting", "گزارش و تحلیل"),
        ("security", "امنیت و دسترسی"),
        ("data", "داده و مهاجرت"),
        ("other", "سایر"),
    ]
    UNKNOWN = "unknown"
    STANDARD = "standard"
    CONFIGURATION = "configuration"
    CUSTOM = "custom"
    INTEGRATION = "integration"
    GAP = "gap"
    FITS = [
        (UNKNOWN, "نیازمند بررسی"),
        (STANDARD, "پوشش استاندارد ERPNext"),
        (CONFIGURATION, "قابل حل با پیکربندی"),
        (CUSTOM, "نیازمند Custom App"),
        (INTEGRATION, "نیازمند اتصال"),
        (GAP, "شکاف تاییدشده"),
    ]
    PRIORITIES = [("critical", "حیاتی"), ("high", "بالا"), ("medium", "متوسط"),
                  ("low", "پایین")]
    EFFORTS = [(UNKNOWN, "نیازمند برآورد"), ("xs", "کمتر از ۳ نفر-روز"),
               ("s", "۳ تا ۱۰ نفر-روز"), ("m", "۲ تا ۴ هفته"),
               ("l", "۱ تا ۲ ماه"), ("xl", "بیش از ۲ ماه")]
    RISKS = [("low", "کم"), ("medium", "متوسط"), ("high", "بالا")]
    PHASES = [("discovery", "فاز کشف"), ("pilot", "پایلوت"),
              ("later", "پس از پایلوت"), ("out", "خارج از محدوده")]
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    DEFERRED = "deferred"
    STATUSES = [(DRAFT, "پیش‌نویس"), (VALIDATED, "اعتبارسنجی‌شده"),
                (APPROVED, "مصوب"), (DEFERRED, "موکول‌شده")]

    decision = models.ForeignKey(ManagementDecision, on_delete=models.PROTECT,
                                 related_name="fit_gap_items", verbose_name="تصمیم جلسه")
    area = models.CharField("حوزه", max_length=30, choices=AREAS)
    title = models.CharField("عنوان نیاز", max_length=180)
    requirement = models.TextField("نیاز واقعی کسب‌وکار")
    current_process = models.TextField("فرایند یا ابزار فعلی", blank=True)
    evidence = models.TextField("منبع و شاهد نیاز", blank=True)
    fit = models.CharField("وضعیت انطباق", max_length=20, choices=FITS, default=UNKNOWN)
    solution = models.TextField("راهکار پیشنهادی", blank=True)
    acceptance_criteria = models.TextField("معیار پذیرش", blank=True)
    priority = models.CharField("اولویت", max_length=10, choices=PRIORITIES, default="medium")
    effort = models.CharField("برآورد تلاش", max_length=10, choices=EFFORTS, default=UNKNOWN)
    risk = models.CharField("ریسک", max_length=10, choices=RISKS, default="medium")
    phase = models.CharField("فاز هدف", max_length=12, choices=PHASES, default="discovery")
    cost_low = models.DecimalField("حداقل هزینه (تومان)", max_digits=18, decimal_places=0,
                                   null=True, blank=True, validators=[MinValueValidator(0)])
    cost_high = models.DecimalField("حداکثر هزینه (تومان)", max_digits=18, decimal_places=0,
                                    null=True, blank=True, validators=[MinValueValidator(0)])
    owner = models.CharField("مالک بررسی یا اجرا", max_length=160, blank=True)
    status = models.CharField("وضعیت", max_length=12, choices=STATUSES, default=DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name="created_fit_gap_items", verbose_name="ثبت‌کننده")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   related_name="updated_fit_gap_items", verbose_name="آخرین ویرایش‌کننده")
    created_at = models.DateTimeField("زمان ثبت", auto_now_add=True)
    updated_at = models.DateTimeField("آخرین ویرایش", auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]

    @property
    def is_complete(self):
        required = (self.current_process.strip(), self.evidence.strip(), self.solution.strip(),
                    self.acceptance_criteria.strip(), self.owner.strip(), self.cost_low,
                    self.cost_high)
        return bool(self.status != self.DRAFT and self.fit != self.UNKNOWN
                    and self.effort != self.UNKNOWN and all(value is not None and value != ""
                                                            for value in required))

    @property
    def priority_score(self):
        priority = {"critical": 4, "high": 3, "medium": 2, "low": 1}[self.priority]
        fit = {self.UNKNOWN: 5, self.GAP: 5, self.CUSTOM: 4, self.INTEGRATION: 4,
               self.CONFIGURATION: 2, self.STANDARD: 1}[self.fit]
        risk = {"high": 3, "medium": 2, "low": 1}[self.risk]
        return priority * 100 + fit * 10 + risk

    def __str__(self):
        return f"{self.get_area_display()}: {self.title}"

