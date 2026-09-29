from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


def generate_stock_ledger_name():
    return f"MAT-SLE-{uuid4().hex.upper()}"


def generate_stock_entry_name():
    return f"MAT-STE-{timezone.localdate().year}-{uuid4().hex[:10].upper()}"


def current_stock_time():
    return timezone.localtime().time().replace(tzinfo=None)


STOCK_PRECISION = Decimal("0.000000001")


def stock_decimal(value):
    return value.quantize(STOCK_PRECISION, rounding=ROUND_HALF_UP)


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
                descendant_ledger = (
                    StockLedgerEntry.objects.filter(
                        warehouse__company_id=old.company_id,
                        warehouse__lft__gt=old.lft,
                        warehouse__rgt__lt=old.rgt,
                    ).exists()
                    if old.is_group and old.lft and old.rgt
                    else False
                )
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
                if (has_ledger or descendant_ledger) and old.account_id != self.account_id:
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
        allow_repost = kwargs.pop("_allow_repost", False)
        if allow_repost:
            allowed_fields = {
                "is_cancelled", "qty_after_transaction", "incoming_rate",
                "outgoing_rate", "valuation_rate", "stock_value",
                "stock_value_difference", "stock_queue",
            }
            update_fields = set(kwargs.get("update_fields") or ())
            if self._state.adding or not update_fields or not update_fields <= allowed_fields:
                raise ValidationError("Only ledger valuation fields may be reposted.")
            return super().save(*args, **kwargs)
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


class StockEntryType(models.Model):
    class Purpose(models.TextChoices):
        MATERIAL_ISSUE = "Material Issue", "Material Issue"
        MATERIAL_RECEIPT = "Material Receipt", "Material Receipt"
        MATERIAL_TRANSFER = "Material Transfer", "Material Transfer"
        MATERIAL_TRANSFER_FOR_MANUFACTURE = (
            "Material Transfer for Manufacture",
            "Material Transfer for Manufacture",
        )
        MATERIAL_CONSUMPTION_FOR_MANUFACTURE = (
            "Material Consumption for Manufacture",
            "Material Consumption for Manufacture",
        )
        MANUFACTURE = "Manufacture", "Manufacture"
        REPACK = "Repack", "Repack"
        SEND_TO_SUBCONTRACTOR = "Send to Subcontractor", "Send to Subcontractor"
        DISASSEMBLE = "Disassemble", "Disassemble"
        RECEIVE_FROM_CUSTOMER = "Receive from Customer", "Receive from Customer"
        RETURN_RAW_MATERIAL_TO_CUSTOMER = (
            "Return Raw Material to Customer",
            "Return Raw Material to Customer",
        )
        SUBCONTRACTING_DELIVERY = "Subcontracting Delivery", "Subcontracting Delivery"
        SUBCONTRACTING_RETURN = "Subcontracting Return", "Subcontracting Return"

    name = models.CharField(max_length=140, primary_key=True)
    purpose = models.CharField(max_length=50, choices=Purpose.choices)
    add_to_transit = models.BooleanField(default=False)
    batch_split = models.BooleanField(default=False, editable=False)
    is_standard = models.BooleanField(default=False, editable=False)

    class Meta:
        db_table = "stock_entry_type"
        ordering = ("name",)

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Stock entry type name is required."})
        if self.add_to_transit and self.purpose != self.Purpose.MATERIAL_TRANSFER:
            raise ValidationError(
                {"add_to_transit": "Transit is only available for Material Transfer."}
            )
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.purpose != self.purpose and old.stock_entries.exists():
            raise ValidationError("A used stock entry type cannot change purpose.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.stock_entries.exists():
            raise ValidationError("A used stock entry type cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name


class StockEntryQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Edit stock entries through validated model saves.")

    def delete(self):
        if self.exclude(status="Draft").exists():
            raise ValidationError("A submitted stock entry cannot be deleted.")
        return super().delete()


class StockEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = "Draft", "Draft"
        SUBMITTED = "Submitted", "Submitted"
        CANCELLED = "Cancelled", "Cancelled"

    name = models.CharField(
        max_length=140, primary_key=True, default=generate_stock_entry_name, editable=False
    )
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.PROTECT, related_name="stock_entries"
    )
    stock_entry_type = models.ForeignKey(
        StockEntryType, on_delete=models.PROTECT, related_name="stock_entries"
    )
    purpose = models.CharField(
        max_length=50, choices=StockEntryType.Purpose.choices, editable=False
    )
    posting_date = models.DateField(default=timezone.localdate)
    posting_time = models.TimeField(default=current_stock_time)
    from_warehouse = models.ForeignKey(
        Warehouse,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_source_stock_entries",
    )
    to_warehouse = models.ForeignKey(
        Warehouse,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_target_stock_entries",
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_entries",
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_entries",
    )
    finance_book = models.ForeignKey(
        "accounting.FinanceBook",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_entries",
        editable=False,
    )
    perpetual_inventory_at_submit = models.BooleanField(default=False, editable=False)
    is_opening = models.BooleanField(default=False)
    remarks = models.TextField(blank=True)
    total_incoming_value = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    total_outgoing_value = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    value_difference = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    total_amount = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False
    )

    objects = StockEntryQuerySet.as_manager()

    class Meta:
        db_table = "stock_entry"
        ordering = ("-posting_date", "-posting_time", "name")
        indexes = [
            models.Index(fields=("company", "posting_date"), name="ste_company_date_idx"),
            models.Index(fields=("company", "status"), name="ste_company_status_idx"),
        ]

    def clean(self):
        super().clean()
        if self.stock_entry_type_id:
            self.purpose = self.stock_entry_type.purpose
            if self.stock_entry_type.add_to_transit:
                raise ValidationError(
                    "Transit stock entries require the two-step transit workflow."
                )
            supported = {
                StockEntryType.Purpose.MATERIAL_RECEIPT,
                StockEntryType.Purpose.MATERIAL_ISSUE,
                StockEntryType.Purpose.MATERIAL_TRANSFER,
            }
            if self.purpose not in supported:
                raise ValidationError(
                    {"stock_entry_type": "This stock entry purpose is not implemented yet."}
                )
        for field in ("from_warehouse", "to_warehouse"):
            warehouse = getattr(self, field)
            if warehouse and (
                warehouse.company_id != self.company_id
                or warehouse.is_group
                or warehouse.disabled
            ):
                raise ValidationError(
                    {field: "Select an enabled leaf warehouse from this company."}
                )
        if self.project_id and self.project.company_id != self.company_id:
            raise ValidationError({"project": "Project must belong to this company."})
        if self.cost_center_id and (
            self.cost_center.company_id != self.company_id
            or self.cost_center.is_group
            or self.cost_center.disabled
        ):
            raise ValidationError(
                {"cost_center": "Select an enabled leaf cost center from this company."}
            )
        if self.from_warehouse_id and self.from_warehouse_id == self.to_warehouse_id:
            raise ValidationError("Default source and target warehouses must be different.")

    def save(self, *args, **kwargs):
        allow_repost = kwargs.pop("_allow_repost", False)
        if allow_repost:
            allowed_fields = {
                "total_incoming_value", "total_outgoing_value", "value_difference",
                "total_amount", "status",
            }
            update_fields = set(kwargs.get("update_fields") or ())
            if self._state.adding or not update_fields or not update_fields <= allowed_fields:
                raise ValidationError("Only submitted stock entry totals and status may be reposted.")
            return super().save(*args, **kwargs)
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.status != self.Status.DRAFT:
            raise ValidationError("A submitted stock entry cannot be edited.")
        if self.status != self.Status.DRAFT and not getattr(self, "_submitting", False):
            raise ValidationError("Submit stock entries through the stock entry service.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if type(self).objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("A submitted stock entry cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name


class StockEntryDetailQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Edit stock entry rows through validated model saves.")

    def delete(self):
        if self.exclude(stock_entry__status=StockEntry.Status.DRAFT).exists():
            raise ValidationError("Rows of a submitted stock entry cannot be deleted.")
        return super().delete()


class StockEntryDetail(models.Model):
    stock_entry = models.ForeignKey(
        StockEntry, on_delete=models.CASCADE, related_name="items"
    )
    position = models.PositiveIntegerField()
    item = models.ForeignKey(
        "catalog.Item", on_delete=models.PROTECT, related_name="stock_entry_rows"
    )
    source_warehouse = models.ForeignKey(
        Warehouse,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="source_stock_entry_rows",
    )
    target_warehouse = models.ForeignKey(
        Warehouse,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="target_stock_entry_rows",
    )
    qty = models.DecimalField(max_digits=30, decimal_places=9)
    uom = models.ForeignKey(
        "catalog.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="stock_entry_rows",
    )
    conversion_factor = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("1")
    )
    stock_uom = models.ForeignKey(
        "catalog.UnitOfMeasure",
        on_delete=models.PROTECT,
        related_name="stock_entry_stock_rows",
        editable=False,
    )
    transfer_qty = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    basic_rate = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    basic_amount = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    amount = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    valuation_rate = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    allow_zero_valuation_rate = models.BooleanField(default=False)
    actual_qty = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    project = models.ForeignKey(
        "projects.Project",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_entry_rows",
    )
    expense_account = models.ForeignKey(
        "accounting.Account",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_entry_rows",
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="stock_entry_rows",
    )
    description = models.TextField(blank=True)

    objects = StockEntryDetailQuerySet.as_manager()

    class Meta:
        db_table = "stock_entry_detail"
        ordering = ("stock_entry", "position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("stock_entry", "position"), name="unique_stock_entry_row_position"
            )
        ]

    def clean(self):
        super().clean()
        if self.stock_entry_id and StockEntry.objects.filter(
            pk=self.stock_entry_id
        ).exclude(status=StockEntry.Status.DRAFT).exists():
            raise ValidationError("Rows of a submitted stock entry cannot change.")
        if self.position is not None and self.position < 1:
            raise ValidationError({"position": "Row position must be positive."})
        if self.qty is None or self.qty <= 0:
            raise ValidationError({"qty": "Quantity must be positive."})
        if self.conversion_factor is None or self.conversion_factor <= 0:
            raise ValidationError({"conversion_factor": "Conversion factor must be positive."})
        if not self.item_id or not self.stock_entry_id or not self.uom_id:
            return

        if self.basic_rate is None:
            raise ValidationError({"basic_rate": "Basic rate is required."})

        entry = self.stock_entry
        if entry.stock_entry_type_id:
            entry.purpose = entry.stock_entry_type.purpose
        item = self.item
        if item.disabled or not item.is_stock_item:
            raise ValidationError({"item": "Select an enabled stock item."})
        if not self.uom.enabled or not item.stock_uom.enabled:
            raise ValidationError({"uom": "Entry and stock UOMs must be enabled."})
        expected_factor = item.quantity_in_stock_uom(Decimal("1"), self.uom)
        if self.conversion_factor != expected_factor:
            raise ValidationError(
                {"conversion_factor": "Conversion factor does not match the item's UOM setup."}
            )
        self.stock_uom = item.stock_uom
        self.transfer_qty = stock_decimal(self.qty * self.conversion_factor)
        if self.transfer_qty <= 0:
            raise ValidationError(
                {"transfer_qty": "Quantity in the stock UOM must be positive."}
            )
        if self.uom.must_be_whole_number and self.qty != self.qty.to_integral_value():
            raise ValidationError({"qty": "Quantity must be a whole number for this UOM."})
        if (
            self.stock_uom.must_be_whole_number
            and self.transfer_qty != self.transfer_qty.to_integral_value()
        ):
            raise ValidationError(
                {"transfer_qty": "Stock quantity must be a whole number for the stock UOM."}
            )

        if not self.source_warehouse_id and entry.from_warehouse_id:
            self.source_warehouse = entry.from_warehouse
        if not self.target_warehouse_id and entry.to_warehouse_id:
            self.target_warehouse = entry.to_warehouse
        for field in ("source_warehouse", "target_warehouse"):
            warehouse = getattr(self, field)
            if warehouse and (
                warehouse.company_id != entry.company_id
                or warehouse.is_group
                or warehouse.disabled
            ):
                raise ValidationError(
                    {field: "Select an enabled leaf warehouse from the entry company."}
                )

        if entry.purpose == StockEntryType.Purpose.MATERIAL_RECEIPT:
            if self.source_warehouse_id or not self.target_warehouse_id:
                raise ValidationError(
                    "Material Receipt rows require only a target warehouse."
                )
            if self.basic_rate < 0 or (
                self.basic_rate == 0 and not self.allow_zero_valuation_rate
            ):
                raise ValidationError(
                    {"basic_rate": "Receipt rate must be positive unless zero valuation is allowed."}
                )
        elif entry.purpose == StockEntryType.Purpose.MATERIAL_ISSUE:
            if not self.source_warehouse_id or self.target_warehouse_id:
                raise ValidationError("Material Issue rows require only a source warehouse.")
            if self.basic_rate < 0:
                raise ValidationError({"basic_rate": "Basic rate cannot be negative."})
        elif entry.purpose == StockEntryType.Purpose.MATERIAL_TRANSFER:
            if not self.source_warehouse_id or not self.target_warehouse_id:
                raise ValidationError(
                    "Material Transfer rows require source and target warehouses."
                )
            if self.source_warehouse_id == self.target_warehouse_id:
                raise ValidationError("Source and target warehouses must be different.")
            if self.basic_rate < 0:
                raise ValidationError({"basic_rate": "Basic rate cannot be negative."})

        if self.project_id and self.project.company_id != entry.company_id:
            raise ValidationError({"project": "Project must belong to the entry company."})
        if self.expense_account_id:
            account = self.expense_account
            if (
                account.company_id != entry.company_id
                or account.is_group
                or account.disabled
                or account.account_type == "Stock"
                or account.account_currency_id != entry.company.default_currency_id
            ):
                raise ValidationError(
                    {"expense_account": "Select an enabled non-Stock ledger in the company currency."}
                )
        if self.cost_center_id and (
            self.cost_center.company_id != entry.company_id
            or self.cost_center.is_group
            or self.cost_center.disabled
        ):
            raise ValidationError(
                {"cost_center": "Select an enabled leaf cost center from the entry company."}
            )

    def save(self, *args, **kwargs):
        allow_repost = kwargs.pop("_allow_repost", False)
        if allow_repost:
            allowed_fields = {
                "basic_rate", "basic_amount", "amount", "actual_qty", "valuation_rate",
            }
            update_fields = set(kwargs.get("update_fields") or ())
            if self._state.adding or not update_fields or not update_fields <= allowed_fields:
                raise ValidationError("Only submitted stock entry valuation fields may be reposted.")
            return super().save(*args, **kwargs)
        old = type(self).objects.select_related("stock_entry").filter(pk=self.pk).first()
        if old and old.stock_entry.status != StockEntry.Status.DRAFT:
            raise ValidationError("Rows of a submitted stock entry cannot change.")
        if self.item_id:
            self.stock_uom = self.item.stock_uom
            if self.qty is not None and self.conversion_factor is not None:
                self.transfer_qty = stock_decimal(self.qty * self.conversion_factor)
        if self.stock_entry_id:
            if not self.source_warehouse_id and self.stock_entry.from_warehouse_id:
                self.source_warehouse = self.stock_entry.from_warehouse
            if not self.target_warehouse_id and self.stock_entry.to_warehouse_id:
                self.target_warehouse = self.stock_entry.to_warehouse
        self.full_clean()
        if not getattr(self, "_submitting", False):
            self.basic_amount = stock_decimal(self.transfer_qty * self.basic_rate)
            self.amount = self.basic_amount
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if StockEntry.objects.filter(
            pk=self.stock_entry_id
        ).exclude(status=StockEntry.Status.DRAFT).exists():
            raise ValidationError("Rows of a submitted stock entry cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.stock_entry_id} / {self.position}: {self.item_id}"


class ReceiptRateCorrectionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Edit rate corrections through validated model saves.")

    def delete(self):
        if self.exclude(status="Draft").exists():
            raise ValidationError("A submitted rate correction cannot be deleted.")
        return super().delete()


class ReceiptRateCorrection(models.Model):
    class Status(models.TextChoices):
        DRAFT = "Draft", "Draft"
        SUBMITTED = "Submitted", "Submitted"

    stock_entry_detail = models.ForeignKey(
        StockEntryDetail, on_delete=models.PROTECT, related_name="rate_corrections"
    )
    new_rate = models.DecimalField(max_digits=30, decimal_places=9)
    previous_rate = models.DecimalField(
        max_digits=30, decimal_places=9, null=True, blank=True, editable=False
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = ReceiptRateCorrectionQuerySet.as_manager()

    class Meta:
        db_table = "receipt_rate_correction"
        ordering = ("-created_at", "-id")

    def clean(self):
        super().clean()
        if not self.reason or not self.reason.strip():
            raise ValidationError({"reason": "A reason is required."})
        if self.new_rate is None or self.new_rate < 0:
            raise ValidationError({"new_rate": "Rate must be nonnegative."})
        if self.stock_entry_detail_id:
            detail = self.stock_entry_detail
            entry = detail.stock_entry
            if entry.purpose != StockEntryType.Purpose.MATERIAL_RECEIPT:
                raise ValidationError("Rate correction currently supports Material Receipt only.")
            if self.new_rate == 0 and not detail.allow_zero_valuation_rate:
                raise ValidationError({"new_rate": "Zero valuation is not allowed on this row."})

    def save(self, *args, **kwargs):
        submitting = kwargs.pop("_submitting", False)
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.status != self.Status.DRAFT:
            raise ValidationError("A submitted rate correction cannot be edited.")
        if self.status != self.Status.DRAFT and not submitting:
            raise ValidationError("Submit rate corrections through the valuation service.")
        if not submitting:
            self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if type(self).objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("A submitted rate correction cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"Rate correction {self.pk or 'draft'}: {self.stock_entry_detail_id}"


def generate_stock_reconciliation_name():
    return f"MAT-RECO-{uuid4().hex[:16].upper()}"


class StockReconciliationQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Edit stock reconciliations through validated model saves.")

    def delete(self):
        if self.exclude(status="Draft").exists():
            raise ValidationError("A submitted stock reconciliation cannot be deleted.")
        return super().delete()


class StockReconciliation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "Draft", "Draft"
        SUBMITTED = "Submitted", "Submitted"
        CANCELLED = "Cancelled", "Cancelled"

    name = models.CharField(
        max_length=140, primary_key=True, default=generate_stock_reconciliation_name,
        editable=False,
    )
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.PROTECT, related_name="stock_reconciliations"
    )
    posting_date = models.DateField(default=timezone.localdate)
    posting_time = models.TimeField(default=current_stock_time)
    expense_account = models.ForeignKey(
        "accounting.Account", null=True, blank=True, on_delete=models.PROTECT,
        related_name="stock_reconciliations",
    )
    cost_center = models.ForeignKey(
        "accounting.CostCenter", null=True, blank=True, on_delete=models.PROTECT,
        related_name="stock_reconciliations",
    )
    remarks = models.TextField(blank=True)
    total_increase_qty = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    total_decrease_qty = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0"), editable=False
    )
    receipt_entry = models.ForeignKey(
        StockEntry, null=True, blank=True, editable=False, on_delete=models.PROTECT,
        related_name="reconciliations_as_receipt",
    )
    issue_entry = models.ForeignKey(
        StockEntry, null=True, blank=True, editable=False, on_delete=models.PROTECT,
        related_name="reconciliations_as_issue",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, editable=False
    )

    objects = StockReconciliationQuerySet.as_manager()

    class Meta:
        db_table = "stock_reconciliation"
        ordering = ("-posting_date", "-posting_time", "name")

    def clean(self):
        super().clean()
        if self.expense_account_id:
            account = self.expense_account
            if (account.company_id != self.company_id or account.is_group or account.disabled
                    or account.account_type == "Stock"
                    or account.account_currency_id != self.company.default_currency_id):
                raise ValidationError({"expense_account": "Select an enabled non-Stock account in the company currency."})
        if self.cost_center_id:
            center = self.cost_center
            if center.company_id != self.company_id or center.is_group or center.disabled:
                raise ValidationError({"cost_center": "Select an enabled leaf cost center in this company."})

    def save(self, *args, **kwargs):
        lifecycle = kwargs.pop("_lifecycle", False)
        if lifecycle:
            allowed = {"status", "receipt_entry", "issue_entry", "total_increase_qty", "total_decrease_qty"}
            fields = set(kwargs.get("update_fields") or ())
            if self._state.adding or not fields or not fields <= allowed:
                raise ValidationError("Only reconciliation lifecycle fields may be updated internally.")
            return super().save(*args, **kwargs)
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.status != self.Status.DRAFT:
            raise ValidationError("A submitted stock reconciliation cannot be edited.")
        if self.status != self.Status.DRAFT:
            raise ValidationError("Submit or cancel through the reconciliation service.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if type(self).objects.filter(pk=self.pk).exclude(status=self.Status.DRAFT).exists():
            raise ValidationError("A submitted stock reconciliation cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return self.name


class StockReconciliationItemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Edit reconciliation rows through validated model saves.")

    def delete(self):
        if self.exclude(reconciliation__status=StockReconciliation.Status.DRAFT).exists():
            raise ValidationError("Rows of a submitted reconciliation cannot be deleted.")
        return super().delete()


class StockReconciliationItem(models.Model):
    reconciliation = models.ForeignKey(
        StockReconciliation, on_delete=models.CASCADE, related_name="items"
    )
    position = models.PositiveIntegerField()
    item = models.ForeignKey(
        "catalog.Item", on_delete=models.PROTECT, related_name="stock_reconciliation_rows"
    )
    warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="stock_reconciliation_rows"
    )
    counted_qty = models.DecimalField(max_digits=30, decimal_places=9)
    receipt_rate = models.DecimalField(
        max_digits=30, decimal_places=9, default=Decimal("0")
    )
    allow_zero_valuation_rate = models.BooleanField(default=False)
    previous_qty = models.DecimalField(
        max_digits=30, decimal_places=9, null=True, blank=True, editable=False
    )
    difference_qty = models.DecimalField(
        max_digits=30, decimal_places=9, null=True, blank=True, editable=False
    )

    objects = StockReconciliationItemQuerySet.as_manager()

    class Meta:
        db_table = "stock_reconciliation_item"
        ordering = ("reconciliation", "position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("reconciliation", "position"), name="unique_reco_row_position"
            ),
            models.UniqueConstraint(
                fields=("reconciliation", "item", "warehouse"),
                name="unique_reco_item_warehouse",
            ),
        ]

    def clean(self):
        super().clean()
        if self.position is None or self.position < 1:
            raise ValidationError({"position": "Position must be positive."})
        if self.counted_qty is None or self.counted_qty < 0:
            raise ValidationError({"counted_qty": "Counted quantity must be nonnegative."})
        if self.receipt_rate is None or self.receipt_rate < 0:
            raise ValidationError({"receipt_rate": "Receipt rate must be nonnegative."})
        if self.reconciliation_id and self.item_id and self.warehouse_id:
            if self.warehouse.company_id != self.reconciliation.company_id or self.warehouse.is_group or self.warehouse.disabled:
                raise ValidationError({"warehouse": "Select an enabled leaf warehouse from the company."})
            if self.item.disabled or not self.item.is_stock_item or not self.item.stock_uom.enabled:
                raise ValidationError({"item": "Select an enabled stock item."})
            if self.item.stock_uom.must_be_whole_number and self.counted_qty != self.counted_qty.to_integral_value():
                raise ValidationError({"counted_qty": "Quantity must be a whole number for this UOM."})

    def save(self, *args, **kwargs):
        submitting = kwargs.pop("_submitting", False)
        if submitting:
            fields = set(kwargs.get("update_fields") or ())
            if self._state.adding or not fields or not fields <= {"previous_qty", "difference_qty"}:
                raise ValidationError("Only reconciliation result fields may be updated internally.")
            return super().save(*args, **kwargs)
        old = type(self).objects.select_related("reconciliation").filter(pk=self.pk).first()
        if old and old.reconciliation.status != StockReconciliation.Status.DRAFT:
            raise ValidationError("Rows of a submitted reconciliation cannot be edited.")
        if self.reconciliation_id and self.reconciliation.status != StockReconciliation.Status.DRAFT:
            raise ValidationError("Rows of a submitted reconciliation cannot be added.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if StockReconciliation.objects.filter(pk=self.reconciliation_id).exclude(status=StockReconciliation.Status.DRAFT).exists():
            raise ValidationError("Rows of a submitted reconciliation cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.reconciliation_id} / {self.position}: {self.item_id}"
