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

