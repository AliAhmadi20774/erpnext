from collections import Counter

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Item, Order, StockMovement


@transaction.atomic
def confirm_order(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.DRAFT:
        raise ValidationError("این سفارش قبلا تایید شده است.")
    lines = list(order.lines.select_related("item"))
    if not lines:
        raise ValidationError("سفارش بدون کالا قابل تایید نیست.")

    totals = Counter()
    for line in lines:
        totals[line.item_id] += line.quantity
    items = {item.pk: item for item in Item.objects.select_for_update().filter(pk__in=totals).order_by("pk")}
    if order.kind == Order.SALES:
        for item_id, quantity in totals.items():
            if items[item_id].stock < quantity:
                raise ValidationError(f"موجودی «{items[item_id].name}» کافی نیست.")

    direction = -1 if order.kind == Order.SALES else 1
    for item_id, quantity in totals.items():
        item = items[item_id]
        item.stock += direction * quantity
        item.save(update_fields=["stock"])
    StockMovement.objects.bulk_create([
        StockMovement(item_id=line.item_id, order=order, change=direction * line.quantity)
        for line in lines
    ])
    order.status = Order.CONFIRMED
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])
    return order

