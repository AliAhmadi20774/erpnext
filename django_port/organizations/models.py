from django.core.exceptions import ValidationError
from django.db import models

from geo.models import Country, Currency


class Company(models.Model):
    class ValuationMethod(models.TextChoices):
        FIFO = "FIFO", "FIFO"
        MOVING_AVERAGE = "Moving Average", "Moving Average"
        LIFO = "LIFO", "LIFO"

    name = models.CharField(max_length=140, primary_key=True)
    abbr = models.CharField(max_length=140, blank=True, unique=True)
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="companies")
    default_currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="default_for_companies"
    )
    default_finance_book = models.ForeignKey(
        "accounting.FinanceBook", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_for_companies",
    )
    enable_perpetual_inventory = models.BooleanField(default=True)
    default_inventory_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_inventory_for_companies",
    )
    stock_adjustment_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="stock_adjustment_for_companies",
    )
    default_warehouse = models.ForeignKey(
        "stock.Warehouse", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_for_companies",
    )
    default_in_transit_warehouse = models.ForeignKey(
        "stock.Warehouse", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_in_transit_for_companies",
    )
    default_receivable_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_receivable_for_companies",
    )
    default_payable_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_payable_for_companies",
    )
    default_advance_received_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_advance_received_for_companies",
    )
    default_advance_paid_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_advance_paid_for_companies",
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter", null=True, blank=True, on_delete=models.PROTECT,
        related_name="default_for_companies",
    )
    reporting_currency = models.ForeignKey(
        Currency,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reporting_for_companies",
    )
    is_group = models.BooleanField(default=False)
    parent_company = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    valuation_method = models.CharField(
        max_length=20, choices=ValuationMethod.choices, default=ValuationMethod.FIFO
    )
    create_chart_of_accounts_based_on = models.CharField(
        max_length=50,
        blank=True,
        choices=(
            ("Standard Template", "Standard Template"),
            ("Existing Company", "Existing Company"),
        ),
    )
    chart_of_accounts = models.CharField(max_length=140, blank=True)
    existing_company = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="descendant_chart_companies",
    )
    tax_id = models.CharField(max_length=140, blank=True)
    date_of_establishment = models.DateField(null=True, blank=True)
    date_of_incorporation = models.DateField(null=True, blank=True)
    date_of_commencement = models.DateField(null=True, blank=True)
    phone_no = models.CharField(max_length=140, blank=True)
    fax = models.CharField(max_length=140, blank=True)
    email = models.EmailField(blank=True)
    website = models.CharField(max_length=140, blank=True)
    company_description = models.TextField(blank=True)

    class Meta:
        db_table = "company"
        verbose_name_plural = "companies"

    def clean(self):
        super().clean()
        if self.cost_center_id:
            cost_center = self.cost_center
            if cost_center.company_id != self.pk or cost_center.is_group or cost_center.disabled:
                raise ValidationError({"cost_center": "Select an enabled leaf cost center from this company."})
        for field in (
            "default_receivable_account", "default_payable_account",
            "default_advance_received_account", "default_advance_paid_account",
            "default_inventory_account",
            "stock_adjustment_account",
        ):
            account = getattr(self, field)
            if account and (account.company_id != self.pk or account.is_group or account.disabled):
                raise ValidationError({field: "Select an enabled ledger account from this company."})
            if field == "default_inventory_account" and account and account.account_type != "Stock":
                raise ValidationError({field: "Select an enabled Stock ledger account."})
            if field == "stock_adjustment_account" and account and (
                account.account_type == "Stock"
                or account.account_currency_id != self.default_currency_id
            ):
                raise ValidationError(
                    {field: "Select a non-Stock ledger account in the company currency."}
                )
        for field in ("default_warehouse", "default_in_transit_warehouse"):
            warehouse = getattr(self, field)
            if warehouse and (
                warehouse.company_id != self.pk or warehouse.is_group or warehouse.disabled
            ):
                raise ValidationError(
                    {field: "Select an enabled leaf warehouse from this company."}
                )
        if (
            self.default_in_transit_warehouse_id
            and self.default_in_transit_warehouse.warehouse_type_id != "Transit"
        ):
            raise ValidationError(
                {"default_in_transit_warehouse": "Select a warehouse with the Transit type."}
            )
        if not self.abbr and self.name:
            self.abbr = "".join(word[0] for word in self.name.split()).upper()
        self.abbr = self.abbr.strip()
        if not self.abbr:
            raise ValidationError({"abbr": "Company abbreviation is required."})

        if self.parent_company_id:
            if self.parent_company_id == self.name:
                raise ValidationError({"parent_company": "A company cannot be its own parent."})
            parent = self.parent_company
            if not parent.is_group:
                raise ValidationError({"parent_company": "Parent company must be a group company."})
            seen = {self.name}
            while parent:
                if parent.name in seen:
                    raise ValidationError({"parent_company": "Company hierarchy cannot contain a cycle."})
                seen.add(parent.name)
                parent = parent.parent_company

        if self.parent_company_id:
            if not self.create_chart_of_accounts_based_on and not self.chart_of_accounts:
                self.create_chart_of_accounts_based_on = "Existing Company"
            if not self.existing_company_id and self.create_chart_of_accounts_based_on == "Existing Company":
                self.existing_company = self.parent_company

        if self.create_chart_of_accounts_based_on == "Existing Company":
            self.chart_of_accounts = ""
            if not self.existing_company_id:
                raise ValidationError({"existing_company": "Please select Existing Company for creating Chart of Accounts."})
            if self.existing_company_id == self.name:
                raise ValidationError({"existing_company": "A company cannot use itself as existing company."})
        elif self.create_chart_of_accounts_based_on == "Standard Template" or self.chart_of_accounts:
            self.create_chart_of_accounts_based_on = "Standard Template"
            self.existing_company = None
            from accounting.chart_import import get_charts_for_country, validate_chart_template
            if not self.chart_of_accounts:
                charts = get_charts_for_country(self.country, with_standard=False)
                self.chart_of_accounts = charts[0] if len(charts) == 1 else "Standard"
            elif not validate_chart_template(self.chart_of_accounts):
                raise ValidationError({"chart_of_accounts": f"Unknown chart template: {self.chart_of_accounts}"})

        if not self.reporting_currency_id:
            if self.parent_company_id and self.parent_company.reporting_currency_id:
                self.reporting_currency_id = self.parent_company.reporting_currency_id
            else:
                self.reporting_currency_id = self.default_currency_id

    def save(self, *args, create_chart=None, **kwargs):
        is_new = self._state.adding
        if not is_new:
            old = type(self).objects.filter(pk=self.pk).values(
                "valuation_method", "default_inventory_account_id"
            ).first()
            if old and (
                old["valuation_method"] != self.valuation_method
                or old["default_inventory_account_id"] != self.default_inventory_account_id
            ):
                from stock.models import StockLedgerEntry

                if StockLedgerEntry.objects.filter(company_id=self.pk).exists():
                    raise ValidationError(
                        "Company valuation method and default inventory account cannot change after stock ledger activity."
                    )
        self.full_clean()
        result = super().save(*args, **kwargs)
        if is_new:
            should_create = (
                create_chart if create_chart is not None
                else bool(self.create_chart_of_accounts_based_on)
            )
            if should_create:
                self.create_default_accounts()
        return result

    def create_default_accounts(self, chart_template=None, existing_company=None):
        from accounting.chart_import import install_chart_for_company

        return install_chart_for_company(
            self,
            chart_template=chart_template or self.chart_of_accounts,
            existing_company=existing_company or self.existing_company,
        )

    def __str__(self):
        return self.name
