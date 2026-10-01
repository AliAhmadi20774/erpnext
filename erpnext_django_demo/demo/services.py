from collections import Counter
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, Fulfillment, Invoice, Item, Order, Payment, StockMovement
from .accounting import (post_fulfillment, post_invoice, post_opening_stock, post_payment,
                         post_stock_adjustment)


def record_audit(actor, action, obj, label, details=None):
    return AuditEvent.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        object_type=obj._meta.model_name,
        object_id=str(obj.pk),
        object_label=label,
        details=details or {},
    )


@transaction.atomic
def confirm_order(order_id, actor=None):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.DRAFT:
        raise ValidationError("فقط پیش‌نویس قابل تایید است.")
    if not order.lines.exists():
        raise ValidationError("سفارش بدون کالا قابل تایید نیست.")
    order.status = Order.CONFIRMED
    order.confirmed_at = timezone.now()
    order.save(update_fields=["status", "confirmed_at"])
    record_audit(actor, "order_confirmed", order, order.number, {"kind": order.kind})
    return order


@transaction.atomic
def fulfill_order(order_id, actor=None):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.CONFIRMED:
        raise ValidationError("ابتدا سفارش را تایید کنید.")
    if Fulfillment.objects.filter(order=order).exists():
        raise ValidationError("تحویل یا دریافت این سفارش قبلا انجام شده است.")
    lines = list(order.lines.select_related("item").order_by("pk"))
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
    movements = []
    for line in lines:
        item = items[line.item_id]
        before = item.stock
        item.stock += direction * line.quantity
        movements.append(StockMovement(
            item_id=line.item_id, order=order, change=direction * line.quantity,
            balance_before=before, balance_after=item.stock,
            source=StockMovement.SALES if order.kind == Order.SALES else StockMovement.PURCHASE,
        ))
    for item in items.values():
        item.save(update_fields=["stock"])
    StockMovement.objects.bulk_create(movements)
    post_fulfillment(fulfillment, actor)
    record_audit(actor, "order_fulfilled", order, order.number, {
        "kind": order.kind,
        "lines": [{"item_id": item_id, "quantity": quantity} for item_id, quantity in totals.items()],
    })
    return fulfillment


@transaction.atomic
def record_opening_stock(item_id, quantity):
    item = Item.objects.select_for_update().get(pk=item_id)
    quantity = int(quantity)
    if quantity < 0 or item.stock != 0 or item.movements.exists():
        raise ValidationError("موجودی افتتاحیه فقط برای کالای جدید و بدون گردش ثبت می‌شود.")
    if quantity == 0:
        return None
    item.stock = quantity
    item.save(update_fields=["stock"])
    movement = StockMovement.objects.create(item=item, source=StockMovement.OPENING,
                                            change=quantity, balance_before=0, balance_after=quantity,
                                            note="موجودی هنگام تعریف کالا")
    post_opening_stock(movement)
    return movement


@transaction.atomic
def adjust_stock(item_id, new_stock, reason, actor=None):
    item = Item.objects.select_for_update().get(pk=item_id)
    new_stock = int(new_stock)
    reason = reason.strip()
    if new_stock < 0:
        raise ValidationError("موجودی نمی‌تواند منفی باشد.")
    if not reason:
        raise ValidationError("دلیل اصلاح موجودی را وارد کنید.")
    if new_stock == item.stock:
        raise ValidationError("موجودی جدید با موجودی فعلی برابر است.")
    before = item.stock
    item.stock = new_stock
    item.save(update_fields=["stock"])
    movement = StockMovement.objects.create(item=item, source=StockMovement.ADJUSTMENT,
                                            change=new_stock - before, balance_before=before,
                                            balance_after=new_stock, note=reason)
    record_audit(actor, "stock_adjusted", item, str(item), {
        "before": before, "after": new_stock, "reason": reason,
    })
    post_stock_adjustment(movement, actor)
    return movement


@transaction.atomic
def issue_invoice(order_id, actor=None):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status != Order.CONFIRMED or not Fulfillment.objects.filter(order=order).exists():
        raise ValidationError("ابتدا تحویل یا دریافت کالا را ثبت کنید.")
    if Invoice.objects.filter(order=order).exists():
        raise ValidationError("صورتحساب این سفارش قبلا صادر شده است.")
    amount = order.total
    if amount <= 0:
        raise ValidationError("مبلغ سفارش باید بیشتر از صفر باشد.")
    invoice = Invoice.objects.create(order=order, amount=amount)
    record_audit(actor, "invoice_issued", invoice, invoice.number, {
        "order_id": order.pk, "kind": order.kind, "amount": str(amount),
    })
    post_invoice(invoice, actor)
    return invoice


@transaction.atomic
def record_payment(invoice_id, amount, reference="", actor=None, idempotency_key=None):
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    if idempotency_key:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            if existing.invoice_id != invoice.pk:
                raise ValidationError("شناسهٔ درخواست پرداخت نامعتبر است.")
            return existing
    amount = Decimal(amount)
    if amount <= 0:
        raise ValidationError("مبلغ باید بیشتر از صفر باشد.")
    if amount > invoice.balance:
        raise ValidationError("مبلغ از ماندهٔ صورتحساب بیشتر است.")
    values = {"invoice": invoice, "amount": amount, "reference": reference.strip()}
    if idempotency_key:
        values["idempotency_key"] = idempotency_key
    payment = Payment.objects.create(**values)
    record_audit(actor, "payment_recorded", payment, invoice.number, {
        "invoice_id": invoice.pk, "order_id": invoice.order_id,
        "kind": invoice.order.kind, "amount": str(amount), "reference": reference.strip(),
    })
    post_payment(payment, actor)
    return payment


@transaction.atomic
def cancel_order(order_id, actor=None):
    order = Order.objects.select_for_update().get(pk=order_id)
    if order.status not in (Order.DRAFT, Order.CONFIRMED):
        raise ValidationError("این سفارش قابل لغو نیست.")
    if Fulfillment.objects.filter(order=order).exists() or Invoice.objects.filter(order=order).exists():
        raise ValidationError("پس از تحویل، دریافت یا صدور صورتحساب نمی‌توان سفارش را لغو کرد.")
    order.status = Order.CANCELLED
    order.cancelled_at = timezone.now()
    order.save(update_fields=["status", "cancelled_at"])
    record_audit(actor, "order_cancelled", order, order.number, {"kind": order.kind})
    return order
