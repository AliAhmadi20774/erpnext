from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class Account(models.Model):
    class RootType(models.TextChoices):
        ASSET = "Asset", "Asset"
        LIABILITY = "Liability", "Liability"
        INCOME = "Income", "Income"
        EXPENSE = "Expense", "Expense"
        EQUITY = "Equity", "Equity"

    name = models.CharField(max_length=140, primary_key=True)
    account_name = models.CharField(max_length=140)
    account_number = models.CharField(max_length=140, blank=True)
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="accounts")
    parent_account = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_group = models.BooleanField(default=False)
    root_type = models.CharField(max_length=10, choices=RootType.choices, blank=True)
    report_type = models.CharField(max_length=20, choices=(("Balance Sheet", "Balance Sheet"), ("Profit and Loss", "Profit and Loss")), blank=True)
    account_type = models.CharField(max_length=140, blank=True)
    account_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="accounts")
    disabled = models.BooleanField(default=False)
    lft = models.PositiveIntegerField(default=0, editable=False, db_index=True)
    rgt = models.PositiveIntegerField(default=0, editable=False, db_index=True)

    class Meta:
        db_table = "account"
        ordering = ("company", "lft", "name")
        constraints = [
            models.UniqueConstraint(fields=("company", "account_number"), condition=~Q(account_number=""), name="unique_account_number_per_company"),
        ]

    def clean(self):
        super().clean()
        self.account_name = self.account_name.strip()
        if not self.account_name:
            raise ValidationError({"account_name": "Account name is required."})
        if self.parent_account_id:
            parent = self.parent_account
            if parent.pk == self.pk:
                raise ValidationError({"parent_account": "Account cannot be its own parent."})
            if parent.company_id != self.company_id:
                raise ValidationError({"parent_account": "Parent account must belong to the same company."})
            if not parent.is_group:
                raise ValidationError({"parent_account": "Parent account must be a group."})
            seen = {self.pk}
            while parent:
                if parent.pk in seen:
                    raise ValidationError({"parent_account": "Account tree cannot contain a cycle."})
                seen.add(parent.pk)
                parent = parent.parent_account
            self.root_type = self.parent_account.root_type
            self.report_type = self.parent_account.report_type
        elif not self.is_group:
            raise ValidationError({"is_group": "A root account must be a group."})
        if not self.root_type:
            raise ValidationError({"root_type": "Root type is required."})
        if not self.report_type:
            self.report_type = "Balance Sheet" if self.root_type in ("Asset", "Liability", "Equity") else "Profit and Loss"
        if self.pk and not self.is_group and self.children.exists():
            raise ValidationError({"is_group": "An account with children must remain a group."})
        if self.pk and GLEntry.objects.filter(account_id=self.pk).exists():
            old = type(self).objects.get(pk=self.pk)
            protected = ("account_currency_id", "is_group", "root_type", "report_type")
            if any(getattr(self, field) != getattr(old, field) for field in protected):
                raise ValidationError("Account currency and classification cannot change after ledger posting.")
        if self.pk and (self.is_group or self.disabled):
            if PartyAccount.objects.filter(Q(account_id=self.pk) | Q(advance_account_id=self.pk)).exists():
                raise ValidationError("A party default account must remain an enabled ledger account.")
            from organizations.models import Company

            if Company.objects.filter(
                Q(default_receivable_account_id=self.pk)
                | Q(default_payable_account_id=self.pk)
                | Q(default_advance_received_account_id=self.pk)
                | Q(default_advance_paid_account_id=self.pk)
            ).exists():
                raise ValidationError("A company default account must remain an enabled ledger account.")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if not self.account_currency_id and self.company_id:
                self.account_currency_id = self.company.default_currency_id
            old_company = type(self).objects.filter(pk=self.pk).values_list("company_id", flat=True).first()
            if old_company and old_company != self.company_id:
                raise ValidationError({"company": "Move accounts between companies by recreating the chart."})
            if old_company:
                old_account = type(self).objects.get(pk=self.pk)
                if not old_account.is_group and self.is_group and self.account_type:
                    raise ValidationError({"account_type": "Clear account type before converting a ledger to a group."})
            self.full_clean()
            if self.pk and type(self).objects.filter(parent_account_id=self.pk).exists():
                old = type(self).objects.get(pk=self.pk)
                if old.root_type != self.root_type or old.report_type != self.report_type:
                    raise ValidationError("Change a parent classification only after moving its children.")
            for row in PartyAccount.objects.filter(Q(account_id=self.pk) | Q(advance_account_id=self.pk)):
                if row.account_id == self.pk:
                    row.account = self
                if row.advance_account_id == self.pk:
                    row.advance_account = self
                row.clean()
            result = super().save(*args, **kwargs)
            rebuild_account_tree(self.company_id)
            self.refresh_from_db(fields=("lft", "rgt"))
            return result

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.children.exists():
                raise ValidationError("Delete child accounts first.")
            company_id = self.company_id
            result = super().delete(*args, **kwargs)
            rebuild_account_tree(company_id)
            return result

    def __str__(self):
        return self.name


def rebuild_account_tree(company_id):
    nodes = list(Account.objects.select_for_update().filter(company_id=company_id).order_by("name"))
    children = defaultdict(list)
    for node in nodes:
        children[node.parent_account_id].append(node)
    seen = set()
    counter = 0

    def visit(node):
        nonlocal counter
        if node.pk in seen:
            raise ValidationError("Account tree contains a cycle.")
        seen.add(node.pk)
        counter += 1
        node.lft = counter
        for child in children[node.pk]:
            visit(child)
        counter += 1
        node.rgt = counter

    for root in children[None]:
        visit(root)
    if len(seen) != len(nodes):
        raise ValidationError("Account tree contains disconnected nodes.")
    Account.objects.bulk_update(nodes, ("lft", "rgt"))


class PartyAccount(models.Model):
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="party_accounts")
    customer = models.ForeignKey("parties.Customer", null=True, blank=True, on_delete=models.CASCADE, related_name="accounts")
    supplier = models.ForeignKey("parties.Supplier", null=True, blank=True, on_delete=models.CASCADE, related_name="accounts")
    customer_group = models.ForeignKey("parties.CustomerGroup", null=True, blank=True, on_delete=models.CASCADE, related_name="accounts")
    supplier_group = models.ForeignKey("parties.SupplierGroup", null=True, blank=True, on_delete=models.CASCADE, related_name="accounts")
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="party_defaults")
    advance_account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.PROTECT, related_name="party_advance_defaults")

    class Meta:
        db_table = "party_account"
        constraints = [
            models.CheckConstraint(condition=(
                Q(customer__isnull=False, supplier__isnull=True, customer_group__isnull=True, supplier_group__isnull=True)
                | Q(customer__isnull=True, supplier__isnull=False, customer_group__isnull=True, supplier_group__isnull=True)
                | Q(customer__isnull=True, supplier__isnull=True, customer_group__isnull=False, supplier_group__isnull=True)
                | Q(customer__isnull=True, supplier__isnull=True, customer_group__isnull=True, supplier_group__isnull=False)
            ), name="party_account_one_party"),
            models.UniqueConstraint(fields=("customer", "company"), condition=Q(customer__isnull=False), name="unique_customer_account_per_company"),
            models.UniqueConstraint(fields=("supplier", "company"), condition=Q(supplier__isnull=False), name="unique_supplier_account_per_company"),
            models.UniqueConstraint(fields=("customer_group", "company"), condition=Q(customer_group__isnull=False), name="unique_customer_group_account_per_company"),
            models.UniqueConstraint(fields=("supplier_group", "company"), condition=Q(supplier_group__isnull=False), name="unique_supplier_group_account_per_company"),
        ]

    def clean(self):
        super().clean()
        party_fields = ("customer", "supplier", "customer_group", "supplier_group")
        selected = [field for field in party_fields if getattr(self, f"{field}_id")]
        if len(selected) != 1:
            raise ValidationError("Select exactly one customer, supplier, or party group.")
        party = getattr(self, selected[0])
        currencies = []
        for field in ("account", "advance_account"):
            selected = getattr(self, field)
            if selected is None:
                continue
            if selected.company_id != self.company_id:
                raise ValidationError({field: "Account must belong to the selected company."})
            if selected.is_group or selected.disabled:
                raise ValidationError({field: "Select an enabled ledger account."})
            currencies.append(selected.account_currency_id)
            party_currency_id = getattr(party, "default_currency_id", None)
            if party_currency_id and selected.account_currency_id not in (party_currency_id, self.company.default_currency_id):
                raise ValidationError({field: "Account currency must match the party or company currency."})
        if len(set(currencies)) > 1:
            raise ValidationError("Account and advance account must use the same currency.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.customer_id or self.supplier_id or self.customer_group_id or self.supplier_group_id} / {self.company_id}"


class CostCenter(models.Model):
    name = models.CharField(max_length=140, primary_key=True)
    cost_center_name = models.CharField(max_length=140)
    cost_center_number = models.CharField(max_length=140, blank=True)
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="cost_centers")
    parent_cost_center = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_group = models.BooleanField(default=False)
    disabled = models.BooleanField(default=False)
    lft = models.PositiveIntegerField(default=0, editable=False, db_index=True)
    rgt = models.PositiveIntegerField(default=0, editable=False, db_index=True)

    class Meta:
        db_table = "cost_center"
        ordering = ("company", "lft", "name")
        constraints = [
            models.UniqueConstraint(fields=("company", "cost_center_number"), condition=~Q(cost_center_number=""), name="unique_cost_center_number_per_company"),
        ]

    def clean(self):
        super().clean()
        self.cost_center_name = self.cost_center_name.strip()
        if not self.cost_center_name:
            raise ValidationError({"cost_center_name": "Cost center name is required."})
        if self.parent_cost_center_id:
            parent = self.parent_cost_center
            if parent.pk == self.pk:
                raise ValidationError({"parent_cost_center": "Cost center cannot be its own parent."})
            if parent.company_id != self.company_id or not parent.is_group:
                raise ValidationError({"parent_cost_center": "Parent must be a group cost center in the same company."})
            if self.cost_center_name == self.company.name:
                raise ValidationError({"cost_center_name": "Company root cannot have a parent."})
            seen = {self.pk}
            while parent:
                if parent.pk in seen:
                    raise ValidationError({"parent_cost_center": "Cost center tree cannot contain a cycle."})
                seen.add(parent.pk)
                parent = parent.parent_cost_center
        else:
            if self.cost_center_name != self.company.name or not self.is_group:
                raise ValidationError("Root cost center must be a group named after the company.")
            if type(self).objects.filter(company=self.company, parent_cost_center__isnull=True).exclude(pk=self.pk).exists():
                raise ValidationError("Only one root cost center is allowed per company.")
        if self.pk and not self.is_group and self.children.exists():
            raise ValidationError({"is_group": "A cost center with children must remain a group."})
        if self.pk and (self.is_group or self.disabled) and GLEntry.objects.filter(cost_center_id=self.pk).exists():
            raise ValidationError("A cost center used in the ledger must remain an enabled leaf.")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if not self.name and self.company_id:
                parts = (self.cost_center_number.strip(), self.cost_center_name.strip(), self.company.abbr)
                self.name = " - ".join(part for part in parts if part)
            old = type(self).objects.filter(pk=self.pk).first()
            if old and old.company_id != self.company_id:
                raise ValidationError({"company": "Move cost centers between companies by recreating them."})
            self.full_clean()
            result = super().save(*args, **kwargs)
            rebuild_cost_center_tree(self.company_id)
            self.refresh_from_db(fields=("lft", "rgt"))
            return result

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.children.exists():
                raise ValidationError("Delete child cost centers first.")
            company_id = self.company_id
            result = super().delete(*args, **kwargs)
            rebuild_cost_center_tree(company_id)
            return result

    def __str__(self):
        return self.name


def rebuild_cost_center_tree(company_id):
    nodes = list(CostCenter.objects.select_for_update().filter(company_id=company_id).order_by("name"))
    if not nodes:
        return
    children = defaultdict(list)
    for node in nodes:
        children[node.parent_cost_center_id].append(node)
    if len(children[None]) != 1:
        raise ValidationError("A company must have exactly one cost center root.")
    seen = set()
    counter = 0

    def visit(node):
        nonlocal counter
        if node.pk in seen:
            raise ValidationError("Cost center tree contains a cycle.")
        seen.add(node.pk)
        counter += 1
        node.lft = counter
        for child in children[node.pk]:
            visit(child)
        counter += 1
        node.rgt = counter

    visit(children[None][0])
    if len(seen) != len(nodes):
        raise ValidationError("Cost center tree contains disconnected nodes.")
    CostCenter.objects.bulk_update(nodes, ("lft", "rgt"))


class FiscalYear(models.Model):
    year = models.CharField(max_length=140, primary_key=True)
    year_start_date = models.DateField()
    year_end_date = models.DateField()
    disabled = models.BooleanField(default=False)
    is_short_year = models.BooleanField(default=False)
    auto_created = models.BooleanField(default=False)
    all_companies = models.BooleanField(default=True)
    companies = models.ManyToManyField("organizations.Company", through="FiscalYearCompany", related_name="fiscal_years", blank=True)

    class Meta:
        db_table = "fiscal_year"
        ordering = ("-year_start_date", "year")

    def clean(self):
        super().clean()
        self.year = (self.year or "").strip()
        if not self.year:
            raise ValidationError({"year": "Fiscal year name is required."})
        if self.year_start_date and self.year_end_date:
            if self.year_start_date > self.year_end_date:
                raise ValidationError({"year_end_date": "End date must be after start date."})
            if not self.is_short_year and self.year_end_date != fiscal_year_end(self.year_start_date):
                raise ValidationError({"year_end_date": "A standard fiscal year must span exactly one year."})
            overlapping = type(self).objects.filter(
                year_start_date__lte=self.year_end_date,
                year_end_date__gte=self.year_start_date,
                all_companies=self.all_companies,
            ).exclude(pk=self.pk)
            if self.all_companies and overlapping.exists():
                raise ValidationError("Global fiscal years cannot overlap.")
            if not self.all_companies and self.pk:
                company_ids = list(self.company_links.values_list("company_id", flat=True))
                if company_ids and overlapping.filter(company_links__company_id__in=company_ids).exists():
                    raise ValidationError("Fiscal years for the same company cannot overlap.")

    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old:
            fixed = ("year_start_date", "year_end_date", "is_short_year", "all_companies")
            if any(getattr(old, field) != getattr(self, field) for field in fixed):
                raise ValidationError("Fiscal year dates and scope cannot change after creation.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.year


def fiscal_year_end(start_date):
    try:
        next_anniversary = start_date.replace(year=start_date.year + 1)
    except ValueError:
        next_anniversary = start_date.replace(year=start_date.year + 1, day=28)
    return next_anniversary - timedelta(days=1)


class FiscalYearCompany(models.Model):
    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.CASCADE, related_name="company_links")
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="fiscal_year_links")

    class Meta:
        db_table = "fiscal_year_company"
        constraints = [
            models.UniqueConstraint(fields=("fiscal_year", "company"), name="unique_fiscal_year_company"),
        ]

    def clean(self):
        super().clean()
        fiscal_year = self.fiscal_year
        if fiscal_year.all_companies:
            raise ValidationError("A global fiscal year cannot have company rows.")
        overlap = FiscalYearCompany.objects.filter(
            company_id=self.company_id,
            fiscal_year__year_start_date__lte=fiscal_year.year_end_date,
            fiscal_year__year_end_date__gte=fiscal_year.year_start_date,
        ).exclude(fiscal_year_id=self.fiscal_year_id)
        if overlap.exists():
            raise ValidationError("Fiscal years for the same company cannot overlap.")

    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and GLEntry.objects.filter(fiscal_year_id=old.fiscal_year_id, company_id=old.company_id).exists():
            if old.fiscal_year_id != self.fiscal_year_id or old.company_id != self.company_id:
                raise ValidationError("A fiscal year company link used by the ledger cannot change.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if GLEntry.objects.filter(fiscal_year_id=self.fiscal_year_id, company_id=self.company_id).exists():
            raise ValidationError("A fiscal year company link used by the ledger cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.fiscal_year_id} / {self.company_id}"


class AccountingPeriod(models.Model):
    name = models.CharField(max_length=300, primary_key=True)
    period_name = models.CharField(max_length=140, unique=True)
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="accounting_periods")
    start_date = models.DateField()
    end_date = models.DateField()
    disabled = models.BooleanField(default=False)
    exempted_role = models.ForeignKey("auth.Group", null=True, blank=True, on_delete=models.PROTECT, related_name="exempted_accounting_periods")

    class Meta:
        db_table = "accounting_period"
        ordering = ("-start_date", "name")
        indexes = [models.Index(fields=("company", "start_date", "end_date"), name="acct_period_company_dates")]

    def clean(self):
        super().clean()
        self.period_name = (self.period_name or "").strip()
        if not self.period_name:
            raise ValidationError({"period_name": "Period name is required."})
        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValidationError({"end_date": "End date must not precede start date."})
            if self.end_date > timezone.localdate():
                raise ValidationError({"end_date": "Accounting period cannot end in the future."})
            if self.company_id and type(self).objects.filter(
                company_id=self.company_id,
                start_date__lte=self.end_date,
                end_date__gte=self.start_date,
            ).exclude(pk=self.pk).exists():
                raise ValidationError("Accounting periods for one company cannot overlap.")

    def save(self, *args, **kwargs):
        from organizations.models import Company

        with transaction.atomic():
            if self.company_id:
                Company.objects.select_for_update().get(pk=self.company_id)
                if not self.name:
                    self.name = f"{self.period_name.strip()} - {self.company.abbr}"
            old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
            if old and (old.company_id != self.company_id or old.period_name != self.period_name):
                raise ValidationError("Rename or move an accounting period through a dedicated workflow.")
            self.full_clean()
            return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ClosedDocument(models.Model):
    accounting_period = models.ForeignKey(AccountingPeriod, on_delete=models.CASCADE, related_name="closed_documents")
    document_type = models.CharField(max_length=140)
    closed = models.BooleanField(default=False)

    class Meta:
        db_table = "closed_document"
        ordering = ("document_type",)
        constraints = [models.UniqueConstraint(fields=("accounting_period", "document_type"), name="unique_closed_document_per_period")]

    def clean(self):
        super().clean()
        from .periods import PERIOD_CLOSING_DOCUMENT_TYPES

        if self.document_type not in PERIOD_CLOSING_DOCUMENT_TYPES:
            raise ValidationError({"document_type": "Select a document type covered by accounting period closing."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.accounting_period_id}: {self.document_type}"


class PeriodClosingVoucherQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Edit period closing vouchers through validated model saves.")

    def delete(self):
        if self.filter(status="Submitted").exists():
            raise ValidationError("A submitted period closing voucher cannot be deleted.")
        return super().delete()


class PeriodClosingVoucher(models.Model):
    class Status(models.TextChoices):
        DRAFT = "Draft", "Draft"
        SUBMITTED = "Submitted", "Submitted"

    name = models.CharField(max_length=140, primary_key=True)
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="period_closing_vouchers")
    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.PROTECT, related_name="period_closing_vouchers")
    closing_account_head = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="period_closing_vouchers")
    transaction_date = models.DateField(null=True, blank=True)
    period_start_date = models.DateField()
    period_end_date = models.DateField()
    remarks = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False)

    objects = PeriodClosingVoucherQuerySet.as_manager()

    class Meta:
        db_table = "period_closing_voucher"
        ordering = ("company", "period_end_date", "name")
        indexes = [models.Index(fields=("company", "period_end_date"), name="pcv_company_end_idx")]

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        self.remarks = (self.remarks or "").strip()
        if not self.name:
            raise ValidationError({"name": "Voucher number is required."})
        if not self.remarks:
            raise ValidationError({"remarks": "Remarks are required."})
        if self.company_id and self.fiscal_year_id and self.period_start_date and self.period_end_date:
            year = self.fiscal_year
            if year.disabled or not (year.year_start_date <= self.period_start_date <= self.period_end_date <= year.year_end_date):
                raise ValidationError("Closing dates must be inside an active fiscal year.")
            if not year.all_companies and not year.company_links.filter(company_id=self.company_id).exists():
                raise ValidationError({"fiscal_year": "Fiscal year does not apply to this company."})
            previous = type(self).objects.filter(
                company_id=self.company_id, fiscal_year_id=self.fiscal_year_id,
                status=self.Status.SUBMITTED, period_end_date__lt=self.period_end_date,
            ).exclude(pk=self.pk).order_by("-period_end_date").first()
            expected_start = previous.period_end_date + timedelta(days=1) if previous else year.year_start_date
            if self.period_start_date != expected_start:
                raise ValidationError({"period_start_date": f"Period must start on {expected_start}."})
            if type(self).objects.filter(
                company_id=self.company_id, status=self.Status.SUBMITTED,
                period_end_date__gt=self.period_end_date,
            ).exclude(pk=self.pk).exists():
                raise ValidationError("A later period closing voucher already exists for this company.")
        if self.company_id and self.closing_account_head_id:
            account = self.closing_account_head
            if account.company_id != self.company_id or account.is_group or account.disabled:
                raise ValidationError({"closing_account_head": "Select an enabled ledger account from this company."})
            if account.root_type not in (Account.RootType.LIABILITY, Account.RootType.EQUITY):
                raise ValidationError({"closing_account_head": "Closing account must be a liability or equity account."})
            if account.account_currency_id != self.company.default_currency_id:
                raise ValidationError({"closing_account_head": "Closing account must use the company currency."})
            if account.account_type in ("Receivable", "Payable"):
                raise ValidationError({"closing_account_head": "A party account cannot be the closing account."})

    def save(self, *args, **kwargs):
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.status == self.Status.SUBMITTED:
            raise ValidationError("A submitted period closing voucher cannot be edited.")
        if self.status != self.Status.DRAFT and not getattr(self, "_submitting", False):
            raise ValidationError("Submit a period closing voucher through the posting service.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.SUBMITTED:
            raise ValidationError("A submitted period closing voucher cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name


class GLEntryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Ledger entries are immutable; post a reversal through a voucher workflow.")

    def delete(self):
        raise ValidationError("Ledger entries are immutable; post a reversal through a voucher workflow.")


class GLEntry(models.Model):
    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="gl_entries")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="gl_entries")
    cost_center = models.ForeignKey(CostCenter, null=True, blank=True, on_delete=models.PROTECT, related_name="gl_entries")
    posting_date = models.DateField(db_index=True)
    fiscal_year = models.ForeignKey(FiscalYear, null=True, blank=True, on_delete=models.PROTECT, related_name="gl_entries")
    transaction_date = models.DateField(null=True, blank=True)
    voucher_type = models.CharField(max_length=140)
    voucher_no = models.CharField(max_length=140)
    account_currency = models.ForeignKey("geo.Currency", on_delete=models.PROTECT, related_name="gl_entries")
    debit = models.DecimalField(max_digits=21, decimal_places=9, default=Decimal("0"))
    credit = models.DecimalField(max_digits=21, decimal_places=9, default=Decimal("0"))
    debit_in_account_currency = models.DecimalField(max_digits=21, decimal_places=9, default=Decimal("0"))
    credit_in_account_currency = models.DecimalField(max_digits=21, decimal_places=9, default=Decimal("0"))
    account_exchange_rate = models.DecimalField(max_digits=21, decimal_places=9, default=Decimal("1"))
    customer = models.ForeignKey("parties.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="gl_entries")
    supplier = models.ForeignKey("parties.Supplier", null=True, blank=True, on_delete=models.PROTECT, related_name="gl_entries")
    is_opening = models.BooleanField(default=False)
    is_advance = models.BooleanField(default=False)
    is_cancelled = models.BooleanField(default=False)
    against = models.TextField(blank=True)
    against_voucher_type = models.CharField(max_length=140, blank=True)
    against_voucher = models.CharField(max_length=140, blank=True)
    remarks = models.TextField(blank=True)

    objects = GLEntryQuerySet.as_manager()

    class Meta:
        db_table = "gl_entry"
        ordering = ("posting_date", "id")
        constraints = [
            models.CheckConstraint(condition=Q(debit__gt=0, credit=0) | Q(debit=0, credit__gt=0), name="gl_one_positive_side"),
        ]
        indexes = [
            models.Index(fields=("company", "posting_date"), name="gl_company_date_idx"),
            models.Index(fields=("company", "voucher_type", "voucher_no"), name="gl_voucher_idx"),
            models.Index(fields=("account", "posting_date"), name="gl_account_date_idx"),
        ]

    def clean(self):
        super().clean()
        if not self.fiscal_year_id:
            raise ValidationError({"fiscal_year": "Fiscal year is required for ledger posting."})
        if self.fiscal_year_id and self.company_id and self.posting_date:
            fiscal_year = self.fiscal_year
            if fiscal_year.disabled or not (fiscal_year.year_start_date <= self.posting_date <= fiscal_year.year_end_date):
                raise ValidationError({"fiscal_year": "Fiscal year must be active and contain the posting date."})
            if not fiscal_year.all_companies and not fiscal_year.company_links.filter(company_id=self.company_id).exists():
                raise ValidationError({"fiscal_year": "Fiscal year does not apply to this company."})
        if self.account_id and self.company_id:
            account = self.account
            if account.company_id != self.company_id or account.is_group or account.disabled:
                raise ValidationError({"account": "Select an enabled ledger account from this company."})
            if account.report_type == "Profit and Loss" and not self.cost_center_id:
                raise ValidationError({"cost_center": "Profit and Loss posting requires a cost center."})
            if account.report_type == "Profit and Loss" and self.is_opening:
                raise ValidationError({"account": "Opening entries cannot use Profit and Loss accounts."})
            if not self.account_currency_id:
                self.account_currency_id = account.account_currency_id
            elif self.account_currency_id != account.account_currency_id:
                raise ValidationError({"account_currency": "Account currency does not match the selected account."})
            if account.account_type == "Receivable":
                if not self.customer_id or self.supplier_id:
                    raise ValidationError("A receivable entry requires a customer and no supplier.")
            elif account.account_type == "Payable":
                if not self.supplier_id or self.customer_id:
                    raise ValidationError("A payable entry requires a supplier and no customer.")
            elif account.account_type == "Equity":
                if self.customer_id and self.supplier_id:
                    raise ValidationError("Select at most one party.")
            elif self.customer_id or self.supplier_id:
                raise ValidationError("Party is allowed only on receivable, payable, or equity accounts.")
        if self.cost_center_id:
            cost_center = self.cost_center
            if cost_center.company_id != self.company_id or cost_center.is_group or cost_center.disabled:
                raise ValidationError({"cost_center": "Select an enabled leaf cost center from this company."})
        if self.customer_id and self.customer.disabled:
            raise ValidationError({"customer": "Customer is disabled."})
        if self.supplier_id and self.supplier.disabled:
            raise ValidationError({"supplier": "Supplier is disabled."})
        if self.debit < 0 or self.credit < 0 or (self.debit > 0) == (self.credit > 0):
            raise ValidationError("Exactly one of debit or credit must be positive.")
        if self.is_cancelled:
            raise ValidationError("Cancellation must use a voucher reversal workflow.")
        if self.account_exchange_rate <= 0:
            raise ValidationError({"account_exchange_rate": "Exchange rate must be positive."})
        if self.account_id and self.company_id:
            if self.account.account_currency_id == self.company.default_currency_id and self.account_exchange_rate != 1:
                raise ValidationError({"account_exchange_rate": "Company-currency accounts use rate 1."})
            quantum = Decimal("0.000000001")
            for base_field, account_field in (
                ("debit", "debit_in_account_currency"),
                ("credit", "credit_in_account_currency"),
            ):
                account_amount = getattr(self, account_field)
                expected = (account_amount * self.account_exchange_rate).quantize(quantum, rounding=ROUND_HALF_UP)
                if account_amount < 0 or expected != getattr(self, base_field):
                    raise ValidationError({account_field: "Amount does not match the company amount and exchange rate."})

    def save(self, *args, **kwargs):
        raise ValidationError("Post ledger entries as one balanced voucher through post_gl_entries().")

    def delete(self, *args, **kwargs):
        raise ValidationError("Ledger entries are immutable; post a reversal through a voucher workflow.")

    def __str__(self):
        return f"{self.voucher_type} {self.voucher_no}: {self.account_id}"
