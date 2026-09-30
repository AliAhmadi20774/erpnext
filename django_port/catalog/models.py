from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import F, Q

from geo.models import Country, Currency


class UOMCategory(models.Model):
    # ERPNext uses category_name as the document name.
    name = models.CharField(max_length=140, primary_key=True)

    class Meta:
        db_table = "uom_category"
        verbose_name = "UOM category"
        verbose_name_plural = "UOM categories"

    def __str__(self):
        return self.name


class UnitOfMeasure(models.Model):
    # ERPNext uses uom_name as the document name.
    name = models.CharField(max_length=140, primary_key=True)
    symbol = models.CharField(max_length=140, blank=True)
    common_code = models.CharField(max_length=3, blank=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        UOMCategory,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="units",
    )
    enabled = models.BooleanField(default=True)
    must_be_whole_number = models.BooleanField(default=False)

    class Meta:
        db_table = "uom"
        verbose_name = "unit of measure"
        verbose_name_plural = "units of measure"

    def __str__(self):
        return self.name


class UOMConversionFactor(models.Model):
    category = models.ForeignKey(UOMCategory, on_delete=models.PROTECT, related_name="global_conversions")
    from_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="conversions_from")
    to_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="conversions_to")
    value = models.DecimalField(max_digits=30, decimal_places=15)

    class Meta:
        db_table = "uom_conversion_factor"
        ordering = ("id",)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.from_uom_id} → {self.to_uom_id}: {self.value}"


def get_uom_conv_factor(from_uom, to_uom):
    """Return stock units per source unit, following ERPNext's lookup order."""
    source = from_uom.name if isinstance(from_uom, UnitOfMeasure) else from_uom
    target = to_uom.name if isinstance(to_uom, UnitOfMeasure) else to_uom
    if source == target:
        return Decimal("1")

    factors = UOMConversionFactor.objects
    exact = factors.filter(from_uom_id=source, to_uom_id=target).first()
    if exact:
        return exact.value

    inverse = factors.filter(from_uom_id=target, to_uom_id=source).first()
    if inverse and inverse.value:
        return _rounded_factor(Decimal("1") / inverse.value)

    for first in factors.filter(to_uom_id=target):
        second = factors.filter(from_uom_id=first.from_uom_id, to_uom_id=source).exclude(value=0).first()
        if second:
            return _rounded_factor(first.value / second.value)

    for first in factors.filter(from_uom_id=source):
        second = factors.filter(from_uom_id=target, to_uom_id=first.to_uom_id).exclude(value=0).first()
        if second:
            return _rounded_factor(first.value / second.value)

    return None


def _rounded_factor(value):
    return value.quantize(Decimal("0.000000001"))


class ItemGroupQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if {"name", "parent_item_group", "parent_item_group_id", "is_group"} & kwargs.keys():
            raise ValidationError("Update item-group structure through model.save().")
        return super().update(**kwargs)

    def delete(self):
        total = 0
        details = defaultdict(int)
        with transaction.atomic():
            for group in list(self.order_by("-lft")):
                count, per_model = group.delete()
                total += count
                for model, deleted in per_model.items():
                    details[model] += deleted
        return total, dict(details)


class ItemGroup(models.Model):
    # ERPNext's item_group_name is also the document name.
    name = models.CharField(max_length=140, primary_key=True)
    parent_item_group = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    is_group = models.BooleanField(default=False)
    image = models.CharField(max_length=512, blank=True)
    lft = models.PositiveIntegerField(default=0, editable=False, db_index=True)
    rgt = models.PositiveIntegerField(default=0, editable=False, db_index=True)

    objects = ItemGroupQuerySet.as_manager()

    class Meta:
        db_table = "item_group"
        ordering = ("lft", "name")

    def clean(self):
        super().clean()
        if self.parent_item_group_id:
            if self.parent_item_group_id == self.name:
                raise ValidationError({"parent_item_group": "An item group cannot be its own parent."})
            parent = self.parent_item_group
            if not parent.is_group:
                raise ValidationError({"parent_item_group": "Parent item group must be a group."})
            seen = {self.name}
            while parent:
                if parent.name in seen:
                    raise ValidationError({"parent_item_group": "Item group hierarchy cannot contain a cycle."})
                seen.add(parent.name)
                parent = parent.parent_item_group
        else:
            if ItemGroup.objects.exclude(pk=self.pk).filter(parent_item_group__isnull=True).exists():
                raise ValidationError({"parent_item_group": "Only one root item group is allowed."})
            if not self.is_group:
                raise ValidationError({"is_group": "Root item group must be a group."})

        if not self.is_group and self.pk and self.children.exists():
            raise ValidationError({"is_group": "An item group with children must remain a group."})

    def save(self, *args, **kwargs):
        with transaction.atomic():
            if not self.parent_item_group_id:
                root = ItemGroup.objects.filter(parent_item_group__isnull=True).exclude(pk=self.pk).first()
                if root:
                    self.parent_item_group = root
                    if kwargs.get("update_fields") is not None:
                        kwargs["update_fields"] = set(kwargs["update_fields"]) | {"parent_item_group"}
            self.full_clean()
            result = super().save(*args, **kwargs)
            rebuild_item_group_tree()
            self.refresh_from_db(fields=["lft", "rgt"])
            return result

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.children.exists():
                raise ValidationError("Cannot delete an item group that has children.")
            result = super().delete(*args, **kwargs)
            rebuild_item_group_tree()
            return result

    def descendants(self, include_self=False):
        queryset = ItemGroup.objects.filter(lft__gt=self.lft, rgt__lt=self.rgt)
        if include_self:
            queryset = ItemGroup.objects.filter(lft__gte=self.lft, rgt__lte=self.rgt)
        return queryset.order_by("lft")

    def __str__(self):
        return self.name


def rebuild_item_group_tree():
    nodes = list(ItemGroup.objects.select_for_update().order_by("name"))
    if not nodes:
        return
    roots = [node for node in nodes if node.parent_item_group_id is None]
    if len(roots) != 1:
        raise ValidationError("Item groups must have exactly one root.")

    children = defaultdict(list)
    for node in nodes:
        children[node.parent_item_group_id].append(node)

    visited = set()
    counter = 0

    def visit(node):
        nonlocal counter
        if node.pk in visited:
            raise ValidationError("Item group hierarchy contains a cycle.")
        visited.add(node.pk)
        counter += 1
        node.lft = counter
        for child in children[node.pk]:
            visit(child)
        counter += 1
        node.rgt = counter

    visit(roots[0])
    if len(visited) != len(nodes):
        raise ValidationError("Item group hierarchy contains disconnected nodes.")
    ItemGroup.objects.bulk_update(nodes, ["lft", "rgt"])


class Brand(models.Model):
    # ERPNext uses the brand label as the document name.
    name = models.CharField(max_length=140, primary_key=True)
    description = models.TextField(blank=True)
    image = models.CharField(max_length=512, blank=True)

    class Meta:
        db_table = "brand"
        ordering = ("name",)

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "Brand name is required."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Item(models.Model):
    # ERPNext's item_code is the document name.
    name = models.CharField(max_length=140, primary_key=True)
    item_name = models.CharField(max_length=140, blank=True)
    item_group = models.ForeignKey(ItemGroup, on_delete=models.PROTECT, related_name="items")
    brand = models.ForeignKey(
        Brand, null=True, blank=True, on_delete=models.PROTECT, related_name="items",
    )
    stock_uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="stock_items")
    disabled = models.BooleanField(default=False)
    is_stock_item = models.BooleanField(default=True)
    is_purchase_item = models.BooleanField(default=True)
    is_sales_item = models.BooleanField(default=True)
    description = models.TextField(blank=True)
    image = models.CharField(max_length=512, blank=True)
    end_of_life = models.DateField(default=date(2099, 12, 31))

    class Meta:
        db_table = "item"

    def clean(self):
        super().clean()
        self.name = self.name.strip()
        if not self.name:
            raise ValidationError({"name": "Item code is required."})
        if not self.item_name:
            self.item_name = self.name

    def save(self, *args, **kwargs):
        with transaction.atomic():
            old_stock_uom = None
            old_is_stock_item = None
            if not self._state.adding:
                previous = Item.objects.filter(pk=self.pk).values(
                    "stock_uom_id", "is_stock_item"
                ).first()
                if previous:
                    old_stock_uom = previous["stock_uom_id"]
                    old_is_stock_item = previous["is_stock_item"]
                    from stock.models import StockLedgerEntry

                    has_stock_ledger = StockLedgerEntry.objects.filter(item_id=self.pk).exists()
                    if has_stock_ledger and old_stock_uom != self.stock_uom_id:
                        raise ValidationError(
                            "An item's stock UOM cannot change after stock ledger activity."
                        )
                    if has_stock_ledger and old_is_stock_item and not self.is_stock_item:
                        raise ValidationError(
                            "A ledger item cannot be converted to a non-stock item."
                        )
            self.full_clean()
            result = super().save(*args, **kwargs)
            if old_stock_uom and old_stock_uom != self.stock_uom_id:
                self.uom_conversions.all().delete(allow_base=True)
            ItemUOMConversion.objects.get_or_create(
                item=self,
                uom=self.stock_uom,
                defaults={"conversion_factor": Decimal("1")},
            )
            return result

    def quantity_in_stock_uom(self, quantity, uom):
        uom_name = uom.name if isinstance(uom, UnitOfMeasure) else uom
        factor = self.uom_conversions.filter(uom_id=uom_name).values_list(
            "conversion_factor", flat=True
        ).first()
        if factor is None or factor == 0:
            factor = get_uom_conv_factor(uom_name, self.stock_uom_id)
        if factor is None or factor == 0:
            raise ValidationError(f"No usable conversion factor for {uom_name} on item {self.name}.")
        return Decimal(str(quantity)) * factor

    def __str__(self):
        return self.item_name


class ItemUOMConversionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if {"item", "item_id", "uom", "uom_id", "conversion_factor"} & kwargs.keys():
            raise ValidationError("Update item UOM conversions through model.save().")
        return super().update(**kwargs)

    def delete(self, *, allow_base=False):
        if not allow_base and self.filter(uom_id=F("item__stock_uom_id")).exists():
            raise ValidationError("Cannot delete an item's stock UOM conversion.")
        return super().delete()


class ItemUOMConversion(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="uom_conversions")
    uom = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name="item_conversions")
    conversion_factor = models.DecimalField(
        max_digits=21,
        decimal_places=9,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    objects = ItemUOMConversionQuerySet.as_manager()

    class Meta:
        db_table = "uom_conversion_detail"
        constraints = [models.UniqueConstraint(fields=("item", "uom"), name="unique_item_uom_conversion")]
        ordering = ("id",)

    def clean(self):
        super().clean()
        if self.pk:
            old = ItemUOMConversion.objects.select_related("item").filter(pk=self.pk).first()
            if old and old.uom_id == old.item.stock_uom_id:
                if self.item_id != old.item_id or self.uom_id != old.uom_id:
                    raise ValidationError("Cannot change the stock UOM conversion row.")
        if self.item_id and self.uom_id == self.item.stock_uom_id:
            if self.conversion_factor != Decimal("1"):
                raise ValidationError({"conversion_factor": "Stock UOM conversion factor must be 1."})
        elif self.item_id and self.uom_id and not self.conversion_factor:
            global_factor = get_uom_conv_factor(self.uom_id, self.item.stock_uom_id)
            if global_factor is not None:
                self.conversion_factor = global_factor

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.uom_id == self.item.stock_uom_id:
            raise ValidationError("Cannot delete an item's stock UOM conversion.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.item_id}: {self.uom_id} × {self.conversion_factor}"


class PriceList(models.Model):
    # ERPNext's price_list_name is the document name.
    name = models.CharField(max_length=140, primary_key=True)
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="price_lists")
    enabled = models.BooleanField(default=True)
    buying = models.BooleanField(default=False)
    selling = models.BooleanField(default=False)
    price_not_uom_dependent = models.BooleanField(default=False)

    class Meta:
        db_table = "price_list"

    def clean(self):
        super().clean()
        if not self.buying and not self.selling:
            raise ValidationError("Price list must be applicable for buying or selling.")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.full_clean()
            result = super().save(*args, **kwargs)
            ItemPrice.objects.filter(price_list=self).update(
                currency=self.currency, buying=self.buying, selling=self.selling
            )
            return result

    def __str__(self):
        return self.name


class PriceListCountry(models.Model):
    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="countries")
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="price_lists")

    class Meta:
        db_table = "price_list_country"
        constraints = [
            models.UniqueConstraint(fields=("price_list", "country"), name="unique_price_list_country")
        ]

    def __str__(self):
        return f"{self.price_list_id}: {self.country_id}"


class ItemPrice(models.Model):
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="prices")
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, related_name="item_prices")
    uom = models.ForeignKey(UnitOfMeasure, blank=True, on_delete=models.PROTECT, related_name="item_prices")
    packing_unit = models.IntegerField(default=0)
    item_name = models.CharField(max_length=140, blank=True, editable=False)
    item_description = models.TextField(blank=True, editable=False)
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="item_prices")
    buying = models.BooleanField(default=False, editable=False)
    selling = models.BooleanField(default=False, editable=False)
    price_list_rate = models.DecimalField(max_digits=30, decimal_places=9)
    valid_from = models.DateField(default=date.today, null=True, blank=True)
    valid_upto = models.DateField(null=True, blank=True)
    customer = models.ForeignKey(
        "parties.Customer", null=True, blank=True, on_delete=models.PROTECT,
        related_name="item_prices",
    )
    supplier = models.ForeignKey(
        "parties.Supplier", null=True, blank=True, on_delete=models.PROTECT,
        related_name="item_prices",
    )
    legacy_customer_name = models.CharField(max_length=140, blank=True, editable=False)
    legacy_supplier_name = models.CharField(max_length=140, blank=True, editable=False)
    batch_no = models.CharField(max_length=140, blank=True)
    lead_time_days = models.IntegerField(default=0)
    note = models.TextField(blank=True)
    reference = models.CharField(max_length=140, blank=True)

    class Meta:
        db_table = "item_price"
        ordering = ("id",)

    def clean(self):
        super().clean()
        if not self.item_id or not self.price_list_id:
            return
        if not self.price_list.enabled:
            raise ValidationError({"price_list": "Price list is disabled."})
        if not self.uom_id:
            self.uom_id = self.item.stock_uom_id
        if not self.item.uom_conversions.filter(uom_id=self.uom_id).exists():
            raise ValidationError({"uom": "UOM is not listed for this item."})
        if self.valid_from and self.valid_upto and self.valid_from > self.valid_upto:
            raise ValidationError({"valid_upto": "Valid until must be on or after valid from."})

        self.currency_id = self.price_list.currency_id
        self.buying = self.price_list.buying
        self.selling = self.price_list.selling
        self.item_name = self.item.item_name
        self.item_description = self.item.description
        if self.customer_id:
            self.legacy_customer_name = ""
        if self.supplier_id:
            self.legacy_supplier_name = ""
        if self.selling and not self.buying:
            self.supplier = None
            self.legacy_supplier_name = ""
        if self.buying and not self.selling:
            self.customer = None
            self.legacy_customer_name = ""
        customer_name = self.customer_id or self.legacy_customer_name
        supplier_name = self.supplier_id or self.legacy_supplier_name
        self.reference = supplier_name if self.buying else customer_name

        duplicates = ItemPrice.objects.filter(
            item_id=self.item_id,
            price_list_id=self.price_list_id,
            uom_id=self.uom_id,
            valid_from=self.valid_from,
            valid_upto=self.valid_upto,
            batch_no=self.batch_no,
            packing_unit=self.packing_unit,
        ).exclude(pk=self.pk)
        if any(
            (row.customer_id or row.legacy_customer_name) == customer_name
            and (row.supplier_id or row.legacy_supplier_name) == supplier_name
            for row in duplicates
        ):
            raise ValidationError("An item price with these conditions already exists.")

    def save(self, *args, **kwargs):
        if self.item_id and not self.uom_id:
            self.uom_id = self.item.stock_uom_id
        if self.price_list_id:
            self.currency_id = self.price_list.currency_id
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.item_id} / {self.price_list_id}: {self.price_list_rate}"


def find_item_price(
    item,
    price_list,
    uom,
    *,
    transaction_date=None,
    customer="",
    supplier="",
    batch_no="",
    quantity=None,
):
    """Find a valid exact-UOM item price, preferring newer and more specific rows."""
    item_name = item.name if isinstance(item, Item) else item
    list_name = price_list.name if isinstance(price_list, PriceList) else price_list
    uom_name = uom.name if isinstance(uom, UnitOfMeasure) else uom
    customer = getattr(customer, "name", customer)
    supplier = getattr(supplier, "name", supplier)
    selected_list = PriceList.objects.get(pk=list_name)
    if not selected_list.enabled:
        raise ValidationError("Price list is disabled.")
    when = transaction_date or date.today()
    candidates = ItemPrice.objects.filter(
        item_id=item_name, price_list_id=list_name, uom_id=uom_name
    ).filter(Q(valid_from__isnull=True) | Q(valid_from__lte=when)).filter(
        Q(valid_upto__isnull=True) | Q(valid_upto__gte=when)
    ).filter(Q(batch_no="") | Q(batch_no=batch_no))

    if customer:
        candidates = candidates.filter(
            Q(customer_id=customer) | Q(legacy_customer_name=customer)
            | Q(customer__isnull=True, supplier__isnull=True,
                legacy_customer_name="", legacy_supplier_name="")
        )
    elif supplier:
        candidates = candidates.filter(
            Q(supplier_id=supplier) | Q(legacy_supplier_name=supplier)
            | Q(customer__isnull=True, supplier__isnull=True,
                legacy_customer_name="", legacy_supplier_name="")
        )
    else:
        candidates = candidates.filter(
            customer__isnull=True, supplier__isnull=True,
            legacy_customer_name="", legacy_supplier_name="",
        )

    matches = list(candidates)
    if not matches:
        return None
    selected = max(
        matches,
        key=lambda row: (
            row.valid_from is not None,
            row.valid_from or date.min,
            bool(row.batch_no),
            bool(row.customer_id or row.supplier_id or row.legacy_customer_name or row.legacy_supplier_name),
            row.pk,
        ),
    )
    if quantity is not None and selected.packing_unit:
        if Decimal(str(quantity)) % selected.packing_unit:
            return None
    return selected
