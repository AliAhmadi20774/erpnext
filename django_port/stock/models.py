from collections import defaultdict
from decimal import Decimal
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q


def generate_stock_ledger_name():
    return f"MAT-SLE-{uuid4().hex.upper()}"


class WarehouseType(models.Model):
    name = models.CharField(max_length=140, primary_key=True)
    description = models.TextField(blank=True)

    class Meta:
        db_table = "warehouse_type"
        ordering = ("name",)

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_name = instance.name
        return instance

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Warehouse type name is required."})

    def save(self, *args, **kwargs):
        loaded_name = getattr(self, "_loaded_name", None)
        if not self._state.adding and loaded_name and self.name != loaded_name:
            raise ValidationError("Warehouse type renaming needs a dedicated workflow.")
        self.full_clean()
        result = super().save(*args, **kwargs)
        self._loaded_name = self.name
        return result

    def __str__(self):
        return self.name


class WarehouseQuerySet(models.QuerySet):
    def update(self, **kwargs):
        structural_fields = {
            "name",
            "warehouse_name",
            "company",
            "company_id",
            "parent_warehouse",
            "parent_warehouse_id",
            "is_group",
            "disabled",
            "account",
            "account_id",
            "warehouse_type",
            "warehouse_type_id",
            "default_in_transit_warehouse",
            "default_in_transit_warehouse_id",
            "is_rejected_warehouse",
            "customer",
            "customer_id",
        }
        if structural_fields & kwargs.keys():
            raise ValidationError("Update warehouse structure through model.save().")
        return super().update(**kwargs)

    def delete(self):
        total = 0
        details = defaultdict(int)
        with transaction.atomic():
            for warehouse in list(self.order_by("company_id", "-lft")):
                count, per_model = warehouse.delete()
                total += count
                for model, deleted in per_model.items():
                    details[model] += deleted
        return total, dict(details)


class Warehouse(models.Model):
    name = models.CharField(max_length=140, primary_key=True)
    warehouse_name = models.CharField(max_length=140)
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.PROTECT, related_name="warehouses"
    )
    parent_warehouse = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    is_group = models.BooleanField(default=False)
    disabled = models.BooleanField(default=False)
    account = models.ForeignKey(
        "accounting.Account",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="warehouses",
    )
    warehouse_type = models.ForeignKey(
        WarehouseType,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="warehouses",
    )
    default_in_transit_warehouse = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_for_warehouses",
    )
    is_rejected_warehouse = models.BooleanField(default=False)
    customer = models.ForeignKey(
        "parties.Customer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="warehouses",
    )
    email_id = models.EmailField(blank=True)
    phone_no = models.CharField(max_length=140, blank=True)
    mobile_no = models.CharField(max_length=140, blank=True)
    address_line_1 = models.CharField(max_length=240, blank=True)
    address_line_2 = models.CharField(max_length=240, blank=True)
    city = models.CharField(max_length=140, blank=True)
    state = models.CharField(max_length=140, blank=True)
    pin = models.CharField(max_length=20, blank=True)
    lft = models.PositiveIntegerField(default=0, editable=False, db_index=True)
    rgt = models.PositiveIntegerField(default=0, editable=False, db_index=True)

    objects = WarehouseQuerySet.as_manager()

    class Meta:
        db_table = "warehouse"
        ordering = ("company", "lft", "name")
        indexes = [
            models.Index(
                fields=("company", "parent_warehouse"), name="warehouse_company_parent_idx"
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("company", "warehouse_name"), name="unique_warehouse_name_per_company"
            ),
        ]

    @staticmethod
    def document_name(warehouse_name, company):
        suffix = f" - {company.abbr}"
        return warehouse_name if warehouse_name.endswith(suffix) else f"{warehouse_name}{suffix}"

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_name = instance.name
        return instance

    def clean(self):
        super().clean()
        self.warehouse_name = (self.warehouse_name or "").strip()
        if not self.warehouse_name:
            raise ValidationError({"warehouse_name": "Warehouse name is required."})

        if self.parent_warehouse_id:
            parent = self.parent_warehouse
            if parent.pk == self.pk:
                raise ValidationError({"parent_warehouse": "Warehouse cannot be its own parent."})
            if parent.company_id != self.company_id or not parent.is_group:
                raise ValidationError(
                    {"parent_warehouse": "Parent must be a group warehouse in the same company."}
                )
            seen = {self.pk}
            while parent:
                if parent.pk in seen:
                    raise ValidationError({"parent_warehouse": "Warehouse tree cannot contain a cycle."})
                seen.add(parent.pk)
                parent = parent.parent_warehouse
        if self.pk and not self.is_group and self.children.exists():
            raise ValidationError({"is_group": "A warehouse with children must remain a group."})

        if self.account_id:
            account = self.account
            if account.company_id != self.company_id:
                raise ValidationError({"account": "Account must belong to the warehouse company."})
            if account.is_group or account.disabled or account.account_type != "Stock":
                raise ValidationError({"account": "Select an enabled Stock ledger account."})

        if self.is_group and self.warehouse_type_id:
            raise ValidationError({"warehouse_type": "Warehouse type can only be set on a leaf warehouse."})
        if self.is_group and (self.is_rejected_warehouse or self.customer_id):
            raise ValidationError("Rejected and customer warehouses must be leaf warehouses.")

        if self.default_in_transit_warehouse_id:
            transit = self.default_in_transit_warehouse
            if transit.pk == self.pk:
                raise ValidationError(
                    {"default_in_transit_warehouse": "A warehouse cannot be its own transit warehouse."}
                )
            if transit.company_id != self.company_id or transit.is_group or transit.disabled:
                raise ValidationError(
                    {
                        "default_in_transit_warehouse": (
                            "Select an enabled leaf warehouse from the same company."
                        )
                    }
                )
            if transit.warehouse_type_id != "Transit":
                raise ValidationError(
                    {"default_in_transit_warehouse": "Select a warehouse with the Transit type."}
                )

        if self.pk:
            from organizations.models import Company

            if (self.is_group or self.disabled) and Company.objects.filter(
                Q(default_warehouse_id=self.pk) | Q(default_in_transit_warehouse_id=self.pk)
            ).exists():
                raise ValidationError("A company default warehouse must remain an enabled leaf.")
            if self.warehouse_type_id != "Transit" and Company.objects.filter(
                default_in_transit_warehouse_id=self.pk
            ).exists():
                raise ValidationError("A company transit warehouse must keep the Transit type.")

    def save(self, *args, **kwargs):
        validate_inventory_account = kwargs.pop("validate_inventory_account", True)
        with transaction.atomic():
            from organizations.models import Company

            Company.objects.select_for_update().get(pk=self.company_id)
            loaded_name = getattr(self, "_loaded_name", None)
            if not self._state.adding and loaded_name and self.name != loaded_name:
                raise ValidationError("Warehouse renaming needs a dedicated workflow.")
            if self.company_id and self.warehouse_name:
                expected_name = self.document_name(self.warehouse_name.strip(), self.company)
                if self._state.adding:
                    self.name = expected_name

            old_pk = loaded_name or self.pk
            old = type(self).objects.filter(pk=old_pk).first() if old_pk else None
            if old:
                if old.company_id != self.company_id:
                    raise ValidationError(
                        {"company": "Move warehouses between companies by recreating them."}
                    )
                if old.warehouse_name != self.warehouse_name:
                    raise ValidationError("Warehouse renaming needs a dedicated workflow.")
                has_ledger = StockLedgerEntry.objects.filter(warehouse_id=old.pk).exists()
                has_quantity = any(
                    item_bin.has_quantity_activity()
                    for item_bin in old.bins.select_for_update()
                )
                if (has_ledger or has_quantity) and not old.is_group and self.is_group:
                    raise ValidationError(
                        "A warehouse used by stock balances or the ledger cannot become a group."
                    )
                if has_quantity and not old.disabled and self.disabled:
                    raise ValidationError("A warehouse with quantity activity cannot be disabled.")
                if has_ledger and old.account_id != self.account_id:
                    raise ValidationError(
                        "A warehouse account used by the stock ledger cannot be changed directly."
                    )

            self.full_clean()
            if (
                validate_inventory_account
                and not self.is_group
                and self.company.enable_perpetual_inventory
                and self.effective_account(raise_error=False) is None
            ):
                raise ValidationError(
                    f"Set an account on warehouse {self.name} or a default inventory account "
                    f"for company {self.company_id}."
                )
            result = super().save(*args, **kwargs)
            self._loaded_name = self.name
            rebuild_warehouse_tree(self.company_id)
            self.refresh_from_db(fields=("lft", "rgt"))
            return result

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            from organizations.models import Company

            Company.objects.select_for_update().get(pk=self.company_id)
            if self.children.exists():
                raise ValidationError("Delete child warehouses first.")
            if StockLedgerEntry.objects.filter(warehouse_id=self.pk).exists():
                raise ValidationError("A warehouse with stock ledger entries cannot be deleted.")
            for item_bin in self.bins.select_for_update():
                if item_bin.has_quantity_activity():
                    raise ValidationError(
                        f"Warehouse {self.pk} cannot be deleted while quantities exist for "
                        f"item {item_bin.item_id}."
                    )
                item_bin.delete(_allow_stock_write=True)
            company_id = self.company_id
            result = super().delete(*args, **kwargs)
            rebuild_warehouse_tree(company_id)
            return result

    def effective_account(self, *, raise_error=True):
        warehouse = self
        seen = set()
        while warehouse:
            if warehouse.pk in seen:
                raise ValidationError("Warehouse tree cannot contain a cycle.")
            seen.add(warehouse.pk)
            if warehouse.account_id:
                return warehouse.account
            warehouse = warehouse.parent_warehouse

        if self.company.default_inventory_account_id:
            return self.company.default_inventory_account

        from accounting.models import Account

        stock_accounts = list(
            Account.objects.filter(
                company_id=self.company_id,
                account_type="Stock",
                is_group=False,
                disabled=False,
            )[:2]
        )
        if len(stock_accounts) == 1:
            return stock_accounts[0]
        if raise_error and not self.is_group:
            raise ValidationError(
                f"No unambiguous inventory account is available for warehouse {self.name}."
            )
        return None

    def descendants(self, include_self=False):
        bounds = {"company_id": self.company_id, "lft__gt": self.lft, "rgt__lt": self.rgt}
        if include_self:
            bounds.update(lft__gte=self.lft, rgt__lte=self.rgt)
            bounds.pop("lft__gt")
            bounds.pop("rgt__lt")
        return type(self).objects.filter(**bounds).order_by("lft")

    def __str__(self):
        return self.name


def rebuild_warehouse_tree(company_id):
    nodes = list(
        Warehouse.objects.select_for_update().filter(company_id=company_id).order_by("name")
    )
    if not nodes:
        return

    children = defaultdict(list)
    for node in nodes:
        children[node.parent_warehouse_id].append(node)
    seen = set()
    counter = 0

    def visit(node):
        nonlocal counter
        if node.pk in seen:
            raise ValidationError("Warehouse tree cannot contain a cycle.")
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
        raise ValidationError("Warehouse tree contains disconnected nodes.")
    Warehouse.objects.bulk_update(nodes, ("lft", "rgt"))


class ImmutableStockQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Stock balances and ledger rows can only change through stock services.")

    def delete(self):
        raise ValidationError("Stock balances and ledger rows cannot be bulk deleted.")


class Bin(models.Model):
    item = models.ForeignKey(
        "catalog.Item", on_delete=models.PROTECT, related_name="stock_bins"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="bins"
    )
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.PROTECT, related_name="stock_bins"
    )
    stock_uom = models.ForeignKey(
        "catalog.UnitOfMeasure", on_delete=models.PROTECT, related_name="stock_bins"
    )
    actual_qty = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    planned_qty = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    indented_qty = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    ordered_qty = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    reserved_qty = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    reserved_qty_for_production = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    reserved_qty_for_sub_contract = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    reserved_qty_for_production_plan = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    reserved_stock = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    projected_qty = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    valuation_rate = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    stock_value = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))

    objects = ImmutableStockQuerySet.as_manager()

    class Meta:
        db_table = "bin"
        ordering = ("warehouse", "item")
        constraints = [
            models.UniqueConstraint(
                fields=("item", "warehouse"), name="unique_bin_item_warehouse"
            )
        ]

    def clean(self):
        super().clean()
        if self.warehouse_id and self.company_id != self.warehouse.company_id:
            raise ValidationError({"company": "Bin company must match the warehouse company."})
        if self.warehouse_id and (self.warehouse.is_group or self.warehouse.disabled):
            raise ValidationError({"warehouse": "Bin warehouse must be an enabled leaf."})
        if self.item_id:
            if not self.item.is_stock_item or self.item.disabled:
                raise ValidationError({"item": "Bin item must be an enabled stock item."})
            if self.stock_uom_id != self.item.stock_uom_id:
                raise ValidationError({"stock_uom": "Bin UOM must match the item's stock UOM."})
            if not self.stock_uom.enabled:
                raise ValidationError({"stock_uom": "Bin UOM must be enabled."})

    def set_projected_qty(self):
        self.projected_qty = (
            self.actual_qty
            + self.ordered_qty
            + self.indented_qty
            + self.planned_qty
            - self.reserved_qty
            - self.reserved_qty_for_production
            - self.reserved_qty_for_sub_contract
            - self.reserved_qty_for_production_plan
        )

    def has_quantity_activity(self):
        return any(
            getattr(self, field) != 0
            for field in (
                "actual_qty",
                "planned_qty",
                "indented_qty",
                "ordered_qty",
                "reserved_qty",
                "reserved_qty_for_production",
                "reserved_qty_for_sub_contract",
                "reserved_qty_for_production_plan",
                "reserved_stock",
                "projected_qty",
            )
        )

    def save(self, *args, **kwargs):
        allow_stock_write = kwargs.pop("_allow_stock_write", False)
        if not allow_stock_write:
            raise ValidationError("Update Bin through the stock ledger service.")
        self.set_projected_qty()
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        allow_stock_write = kwargs.pop("_allow_stock_write", False)
        if not allow_stock_write:
            raise ValidationError("Delete Bin through the stock ledger service.")
        if self.has_quantity_activity():
            raise ValidationError("A Bin with quantity activity cannot be deleted.")
        if self.ledger_entries.exists():
            raise ValidationError("A Bin with stock ledger entries cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.item_id} / {self.warehouse_id}"


class StockLedgerEntry(models.Model):
    name = models.CharField(
        max_length=140, primary_key=True, default=generate_stock_ledger_name, editable=False
    )
    item = models.ForeignKey(
        "catalog.Item", on_delete=models.PROTECT, related_name="stock_ledger_entries"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock_ledger_entries"
    )
    item_bin = models.ForeignKey(
        Bin, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.PROTECT, related_name="stock_ledger_entries"
    )
    stock_uom = models.ForeignKey(
        "catalog.UnitOfMeasure", on_delete=models.PROTECT, related_name="stock_ledger_entries"
    )
    fiscal_year = models.ForeignKey(
        "accounting.FiscalYear", on_delete=models.PROTECT, related_name="stock_ledger_entries"
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_ledger_entries",
    )
    posting_date = models.DateField(db_index=True)
    posting_time = models.TimeField()
    posting_datetime = models.DateTimeField(db_index=True)
    creation = models.DateTimeField(auto_now_add=True, editable=False)
    voucher_type = models.CharField(max_length=140)
    voucher_no = models.CharField(max_length=140)
    voucher_detail_no = models.CharField(max_length=140, blank=True)
    actual_qty = models.DecimalField(max_digits=30, decimal_places=9)
    qty_after_transaction = models.DecimalField(max_digits=30, decimal_places=9)
    incoming_rate = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    outgoing_rate = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    valuation_rate = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    stock_value = models.DecimalField(max_digits=30, decimal_places=9, default=Decimal("0"))
    stock_value_difference = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    stock_queue = models.JSONField(default=list, blank=True)
    serial_no = models.TextField(blank=True)
    batch_no = models.CharField(max_length=140, blank=True)
    dependant_sle_voucher_detail_no = models.CharField(max_length=140, blank=True)
    is_cancelled = models.BooleanField(default=False)
    is_adjustment_entry = models.BooleanField(default=False)
    recalculate_rate = models.BooleanField(default=False)

    objects = ImmutableStockQuerySet.as_manager()

    class Meta:
        db_table = "stock_ledger_entry"
        ordering = ("posting_datetime", "creation", "name")
        indexes = [
            models.Index(
                fields=("item", "warehouse", "posting_datetime", "name"),
                name="sle_item_wh_posting_idx",
            ),
            models.Index(
                fields=("voucher_type", "voucher_no"), name="sle_voucher_idx"
            ),
            models.Index(
                fields=("company", "posting_datetime"), name="sle_company_posting_idx"
            ),
        ]

    def clean(self):
        super().clean()
        if not self.actual_qty:
            raise ValidationError({"actual_qty": "Stock ledger quantity must not be zero."})
        if self.warehouse_id and (
            self.warehouse.company_id != self.company_id
            or self.warehouse.is_group
            or self.warehouse.disabled
        ):
            raise ValidationError(
                {"warehouse": "Select an enabled leaf warehouse from the entry company."}
            )
        if self.item_id:
            if not self.item.is_stock_item or self.item.disabled:
                raise ValidationError({"item": "Select an enabled stock item."})
            if self.stock_uom_id != self.item.stock_uom_id:
                raise ValidationError({"stock_uom": "Stock UOM must match the item."})
        if self.item_bin_id and (
            self.item_bin.item_id != self.item_id
            or self.item_bin.warehouse_id != self.warehouse_id
            or self.item_bin.company_id != self.company_id
        ):
            raise ValidationError({"item_bin": "Bin must match the item, warehouse, and company."})
        if self.project_id and self.project.company_id != self.company_id:
            raise ValidationError({"project": "Project must belong to the entry company."})
        if self.fiscal_year_id and not (
            self.fiscal_year.year_start_date
            <= self.posting_date
            <= self.fiscal_year.year_end_date
        ):
            raise ValidationError(
                {"fiscal_year": "Fiscal year must cover the posting date."}
            )
        if self.posting_datetime and (
            self.posting_datetime.date() != self.posting_date
            or self.posting_datetime.time().replace(tzinfo=None) != self.posting_time
        ):
            raise ValidationError(
                {"posting_datetime": "Posting date, time, and datetime must agree."}
            )
        if self.incoming_rate < 0 or self.outgoing_rate < 0 or self.valuation_rate < 0:
            raise ValidationError("Stock rates cannot be negative.")

    def save(self, *args, **kwargs):
        allow_stock_write = kwargs.pop("_allow_stock_write", False)
        if not allow_stock_write:
            raise ValidationError("Create stock ledger rows through the stock ledger service.")
        if not self._state.adding or type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Stock ledger entries are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stock ledger entries are immutable; reverse the source voucher instead.")

    def __str__(self):
        return self.name
