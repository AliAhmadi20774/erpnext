from collections import Counter
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Fulfillment, Invoice, Item, Order, Payment, StockMovement


@transaction.atomic
def confirm_order(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.DRAFT:
        raise ValidationError("فقط پیش‌نویس قابل تایید است.")
    if not order.lines.exists():
        raise ValidationError("سفارش بدون کالا قابل تایید نیست.")
    order.status = Order.CONFIRMED
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])
    return order


@transaction.atomic
def fulfill_order(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.CONFIRMED:
        raise ValidationError("ابتدا سفارش را تایید کنید.")
    if Fulfillment.objects.filter(order=order).exists():
        raise ValidationError("تحویل یا دریافت این سفارش قبلا انجام شده است.")
    lines = list(order.lines.select_related("item"))
    if not lines:
        raise ValidationError("سفارش بدون کالا قابل تحویل یا دریافت نیست.")

    totals = Counter()
    for line in lines:
        totals[line.item_id] += line.quantity
    items = {item.pk: item for item in Item.objects.select_for_update().filter(pk__in=totals).order_by("pk")}
    if order.kind == Order.SALES:
        for item_id, quantity in totals.items():
            if items[item_id].stock < quantity:
                raise ValidationError(f"موجودی «{items[item_id].name}» کافی نیست.")

    direction = -1 if order.kind == Order.SALES else 1
    fulfillment = Fulfillment.objects.create(order=order)
    for item_id, quantity in totals.items():
        item = items[item_id]
        item.stock += direction * quantity
        item.save(update_fields=["stock"])
    StockMovement.objects.bulk_create([
        StockMovement(item_id=line.item_id, order=order, change=direction * line.quantity)
        for line in lines
    ])
    return fulfillment


@transaction.atomic
def issue_invoice(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.CONFIRMED or not Fulfillment.objects.filter(order=order).exists():
        raise ValidationError("ابتدا تحویل یا دریافت کالا را ثبت کنید.")
    if Invoice.objects.filter(order=order).exists():
        raise ValidationError("صورتحساب این سفارش قبلا صادر شده است.")
    amount = order.total
    if amount <= 0:
        raise ValidationError("مبلغ سفارش باید بیشتر از صفر باشد.")
    return Invoice.objects.create(order=order, amount=amount)


@transaction.atomic
def record_payment(invoice_id, amount, reference=""):
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("مبلغ باید بیشتر از صفر باشد.")
    if amount > invoice.balance:
        raise ValidationError("مبلغ از ماندهٔ صورتحساب بیشتر است.")
    return Payment.objects.create(invoice=invoice, amount=amount, reference=reference.strip())


@transaction.atomic
def cancel_order(order_id):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status not in (Order.DRAFT, Order.CONFIRMED):
        raise ValidationError("این سفارش قابل لغو نیست.")
    if Fulfillment.objects.filter(order=order).exists() or Invoice.objects.filter(order=order).exists():
        raise ValidationError("پس از تحویل، دریافت یا صدور صورتحساب نمی‌توان سفارش را لغو کرد.")
    order.status = Order.CANCELLED
    order.cancelled_at = timezone.now()
    order.save(update_fields=["status", "cancelled_at"])
    return order
