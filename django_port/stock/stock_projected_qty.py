"""Current projected stock quantities from Bin balances."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.utils import timezone

from catalog.models import Brand, Item, ItemGroup, ItemUOMConversion, UnitOfMeasure
from organizations.models import Company

from .models import Bin, ItemReorder, Warehouse


PRECISION = Decimal("0.000000001")
CONVERTIBLE_LABELS = (
    "Actual Qty", "Planned Qty", "Requested Qty", "Ordered Qty", "Reserved Qty",
    "Reserved for Production", "Reserved for Production Plan",
    "Reserved for Sub Contracting", "Reserved Stock", "Projected Qty",
    "Reorder Level", "Reorder Qty", "Shortage Qty",
)


@dataclass(frozen=True)
class StockProjectedQtyRow:
    bin: Bin
    reorder_level: Decimal
    reorder_qty: Decimal
    shortage_qty: Decimal
    converted_quantities: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class StockProjectedQtyResult:
    rows: tuple[StockProjectedQtyRow, ...]
    include_uom: str | None = None

    @property
    def converted_headers(self):
        return tuple(f"{label} ({self.include_uom})" for label in CONVERTIBLE_LABELS) if self.include_uom else ()


def stock_projected_qty(*, company=None, item=None, item_group=None, warehouse=None,
                        brand=None, include_uom=None):
    if company is not None and not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
    if brand is not None and not isinstance(brand, Brand):
        raise TypeError("brand must be a Brand instance")
    if include_uom is not None and not isinstance(include_uom, UnitOfMeasure):
        raise TypeError("include_uom must be a UnitOfMeasure instance")
    if item_group is not None:
        if not isinstance(item_group, ItemGroup):
            raise TypeError("item_group must be an ItemGroup instance")
        item_group = ItemGroup.objects.get(pk=item_group.pk)
    if warehouse is not None:
        if not isinstance(warehouse, Warehouse):
            raise TypeError("warehouse must be a Warehouse instance")
        if company is not None and warehouse.company_id != company.pk:
            raise ValidationError("Warehouse must belong to the selected company.")
        warehouse = Warehouse.objects.get(pk=warehouse.pk)

    bins = Bin.objects.filter(
        item__is_stock_item=True, item__disabled=False,
        item__end_of_life__gte=timezone.localdate(),
    )
    if company is not None:
        bins = bins.filter(company=company)
    if item is not None:
        bins = bins.filter(item=item)
    if brand is not None:
        bins = bins.filter(item__brand=brand)
    if item_group is not None:
        bins = bins.filter(item__item_group__lft__gte=item_group.lft,
                           item__item_group__rgt__lte=item_group.rgt)
    if warehouse is not None:
        bins = bins.filter(warehouse__lft__gte=warehouse.lft,
                           warehouse__rgt__lte=warehouse.rgt,
                           warehouse__company=warehouse.company)
    bins = list(bins.select_related(
        "item", "item__item_group", "item__brand", "warehouse", "stock_uom",
    ).order_by("item_id", "warehouse_id"))
    reorder_levels = {
        (setting.item_id, setting.warehouse_id): setting
        for setting in ItemReorder.objects.filter(
            item_id__in={row.item_id for row in bins},
            warehouse_id__in={row.warehouse_id for row in bins},
        )
    }
    factors = {
        conversion.item_id: conversion.conversion_factor
        for conversion in ItemUOMConversion.objects.filter(
            item_id__in={row.item_id for row in bins}, uom=include_uom,
        )
    } if include_uom else {}
    rows = []
    for item_bin in bins:
        setting = reorder_levels.get((item_bin.item_id, item_bin.warehouse_id))
        level = setting.warehouse_reorder_level if setting else Decimal("0")
        qty = setting.warehouse_reorder_qty if setting else Decimal("0")
        shortage = max(level - item_bin.projected_qty, Decimal("0")) if level or qty else Decimal("0")
        converted = ()
        if include_uom:
            factor = factors.get(item_bin.item_id) or Decimal("1")
            quantities = (
                item_bin.actual_qty, item_bin.planned_qty, item_bin.indented_qty,
                item_bin.ordered_qty, item_bin.reserved_qty,
                item_bin.reserved_qty_for_production,
                item_bin.reserved_qty_for_production_plan,
                item_bin.reserved_qty_for_sub_contract, item_bin.reserved_stock,
                item_bin.projected_qty, level, qty, shortage,
            )
            converted = tuple((value / factor).quantize(PRECISION, rounding=ROUND_HALF_UP)
                              for value in quantities)
        rows.append(StockProjectedQtyRow(item_bin, level, qty, shortage, converted))
    return StockProjectedQtyResult(tuple(rows), include_uom.pk if include_uom else None)
