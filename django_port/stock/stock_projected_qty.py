"""Current projected stock quantities from Bin balances."""

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils import timezone

from catalog.models import Item, ItemGroup
from organizations.models import Company

from .models import Bin, Warehouse


@dataclass(frozen=True)
class StockProjectedQtyResult:
    rows: tuple[Bin, ...]


def stock_projected_qty(*, company=None, item=None, item_group=None, warehouse=None):
    if company is not None and not isinstance(company, Company):
        raise TypeError("company must be a Company instance")
    if item is not None and not isinstance(item, Item):
        raise TypeError("item must be an Item instance")
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
    if item_group is not None:
        bins = bins.filter(item__item_group__lft__gte=item_group.lft,
                           item__item_group__rgt__lte=item_group.rgt)
    if warehouse is not None:
        bins = bins.filter(warehouse__lft__gte=warehouse.lft,
                           warehouse__rgt__lte=warehouse.rgt,
                           warehouse__company=warehouse.company)
    return StockProjectedQtyResult(tuple(bins.select_related(
        "item", "item__item_group", "warehouse", "stock_uom",
    ).order_by("item_id", "warehouse_id")))
