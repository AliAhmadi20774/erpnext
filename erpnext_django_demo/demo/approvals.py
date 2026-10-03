from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .access import ROLE_MANAGER, ROLE_PURCHASE, has_role
from .models import Order, PurchasePolicy
from .services import record_audit


def approval_limit():
    return (PurchasePolicy.objects.first() or PurchasePolicy()).approval_limit


def purchase_basis(order):
    return {"supplier_id": order.supplier_id, "lines": [
        {"item_id": row.item_id, "quantity": row.quantity, "price": str(row.unit_price)}
        for row in order.lines.order_by("item_id", "quantity", "unit_price")]}


def approval_needed(order):
    return order.kind == Order.PURCHASE and order.total > approval_limit()


def approval_valid(order):
    return order.approval_status == "approved" and order.approval_basis == purchase_basis(order)


def ensure_purchase_approved(order):
    if approval_needed(order) and not approval_valid(order):
        raise ValidationError("این خرید بالاتر از سقف مبلغ است و به تایید معتبر مدیر نیاز دارد.")


@transaction.atomic
def request_purchase_approval(order_id, reason, actor):
    if not has_role(actor, ROLE_MANAGER, ROLE_PURCHASE):
        raise PermissionDenied
    order = Order.objects.select_for_update().get(pk=order_id)
    if (order.kind != Order.PURCHASE or order.status == Order.CANCELLED
            or hasattr(order, "fulfillment") or not order.lines.exists()):
        raise ValidationError("فقط خرید لغونشده و دریافت‌نشده دارای قلم قابل ارسال است.")
    if not reason.strip():
        raise ValidationError("دلیل خرید را ثبت کنید.")
    if order.approval_status == "pending" and order.approval_basis == purchase_basis(order):
        raise ValidationError("درخواست فعلی از قبل در صف مدیر است.")
    order.approval_status = "pending"
    order.approval_reason = reason.strip()
    order.approval_basis = purchase_basis(order)
    order.approval_requested_at = timezone.now()
    order.approval_decided_at = None
    order.approval_decided_by = None
    order.approval_decision_reason = ""
    order.save()
    record_audit(actor, "purchase_approval_requested", order, order.number,
                 {"reason": reason.strip(), "basis": order.approval_basis, "amount": str(order.total)})
    return order


@transaction.atomic
def decide_purchase_approval(order_id, approved, reason, actor):
    if not has_role(actor, ROLE_MANAGER):
        raise PermissionDenied
    order = Order.objects.select_for_update().get(pk=order_id)
    if (order.approval_status != "pending" or order.status == Order.CANCELLED
            or hasattr(order, "fulfillment") or order.kind != Order.PURCHASE):
        raise ValidationError("درخواست خرید در وضعیت قابل تصمیم نیست.")
    if order.approval_basis != purchase_basis(order):
        raise ValidationError("اقلام خرید تغییر کرده‌اند؛ درخواست باید دوباره ارسال شود.")
    if not reason.strip():
        raise ValidationError("دلیل تصمیم را ثبت کنید.")
    order.approval_status = "approved" if approved else "rejected"
    order.approval_decided_by = actor
    order.approval_decided_at = timezone.now()
    order.approval_decision_reason = reason.strip()
    order.save()
    record_audit(actor, "purchase_approval_decided", order, order.number,
                 {"approved": approved, "reason": reason.strip(), "basis": order.approval_basis})
    return order


def invalidate_purchase_approval(order, actor=None):
    if order.kind == Order.PURCHASE and order.approval_status != "not_requested" and order.approval_basis != purchase_basis(order):
        previous = order.approval_status
        order.approval_status = "not_requested"
        order.save(update_fields=["approval_status"])
        record_audit(actor, "purchase_approval_invalidated", order, order.number,
                     {"previous_status": previous, "old_basis": order.approval_basis,
                      "current_basis": purchase_basis(order)})
