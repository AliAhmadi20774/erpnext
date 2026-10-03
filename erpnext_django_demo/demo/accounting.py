from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Account, AuditEvent, JournalEntry, JournalLine, Order


CHART_OF_ACCOUNTS = (
    ("1100", "بانک و صندوق", Account.ASSET),
    ("1200", "حساب‌های دریافتنی", Account.ASSET),
    ("1300", "موجودی کالا", Account.ASSET),
    ("1400", "کار در جریان ساخت", Account.ASSET),
    ("2100", "حساب‌های پرداختنی", Account.LIABILITY),
    ("2200", "کالای دریافت‌شده صورتحساب‌نشده", Account.LIABILITY),
    ("3100", "تراز افتتاحیه", Account.EQUITY),
    ("4100", "درآمد فروش", Account.INCOME),
    ("5100", "بهای تمام‌شدهٔ کالای فروش‌رفته", Account.EXPENSE),
    ("5200", "کسری و اضافات انبار", Account.EXPENSE),
    ("5300", "هزینهٔ مردودی کنترل کیفیت", Account.EXPENSE),
)


def ensure_chart_of_accounts():
    result = {}
    for code, name, account_type in CHART_OF_ACCOUNTS:
        account, _ = Account.objects.get_or_create(
            code=code, defaults={"name": name, "account_type": account_type})
        result[code] = account
    return result


@transaction.atomic
def post_journal(*, source_type, source_id, source_label, description, posted_at, rows, actor=None):
    existing = JournalEntry.objects.filter(source_type=source_type, source_id=source_id).first()
    if existing:
        return existing
    normalized = [(code, Decimal(debit), Decimal(credit), memo)
                  for code, debit, credit, memo in rows if Decimal(debit) or Decimal(credit)]
    total_debit = sum((debit for _, debit, _, _ in normalized), Decimal("0"))
    total_credit = sum((credit for _, _, credit, _ in normalized), Decimal("0"))
    if not normalized or total_debit <= 0 or total_debit != total_credit:
        raise ValidationError("سند حسابداری باید دارای بدهکار و بستانکار مساوی و مثبت باشد.")
    accounts = ensure_chart_of_accounts()
    entry = JournalEntry.objects.create(
        source_type=source_type, source_id=source_id, source_label=source_label,
        description=description, posted_at=posted_at,
        posted_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    JournalLine.objects.bulk_create([
        JournalLine(entry=entry, account=accounts[code], debit=debit, credit=credit, memo=memo)
        for code, debit, credit, memo in normalized
    ])
    AuditEvent.objects.create(
        actor=entry.posted_by, action="journal_posted", object_type=entry._meta.model_name,
        object_id=str(entry.pk), object_label=entry.number,
        details={"source_type": source_type, "source_id": source_id,
                 "debit": str(total_debit), "credit": str(total_credit)},
    )
    return entry


def post_opening_stock(movement, actor=None):
    amount = Decimal(movement.change) * movement.item.purchase_price
    if amount <= 0:
        return None
    return post_journal(
        source_type="opening_stock", source_id=movement.pk, source_label=f"OPEN-{movement.item.sku}",
        description=f"موجودی افتتاحیهٔ {movement.item.name}", posted_at=movement.created_at,
        rows=[("1300", amount, 0, movement.item.name),
              ("3100", 0, amount, "تراز افتتاحیه")], actor=actor,
    )


def post_fulfillment(fulfillment, actor=None):
    order = fulfillment.order
    if order.kind == Order.SALES:
        batches = list(order.fulfillment_batches.all())
        amount = (sum((row.quantity * row.unit_cost for batch in batches
                       for row in batch.lines.all()), Decimal(0)) if batches else
                  sum((Decimal(line.quantity) * line.item.purchase_price
                       for line in order.lines.select_related("item")), Decimal(0)))
        rows = [("5100", amount, 0, order.number), ("1300", 0, amount, order.number)]
        description = f"بهای تمام‌شدهٔ تحویل {order.number}"
    else:
        amount = order.total
        rows = [("1300", amount, 0, order.number), ("2200", 0, amount, order.number)]
        description = f"دریافت کالای {order.number}"
    if amount <= 0:
        return None
    return post_journal(
        source_type="fulfillment", source_id=fulfillment.pk, source_label=str(fulfillment),
        description=description, posted_at=fulfillment.completed_at, rows=rows, actor=actor,
    )


def post_invoice(invoice, actor=None):
    if invoice.order.kind == Order.SALES:
        rows = [("1200", invoice.amount, 0, invoice.number),
                ("4100", 0, invoice.amount, invoice.number)]
        description = f"صورتحساب فروش {invoice.number}"
    else:
        rows = [("2200", invoice.amount, 0, invoice.number),
                ("2100", 0, invoice.amount, invoice.number)]
        description = f"صورتحساب خرید {invoice.number}"
    return post_journal(
        source_type="invoice", source_id=invoice.pk, source_label=invoice.number,
        description=description, posted_at=invoice.issued_at, rows=rows, actor=actor,
    )


def post_payment(payment, actor=None):
    invoice = payment.invoice
    if invoice.order.kind == Order.SALES:
        rows = [("1100", payment.amount, 0, payment.reference or invoice.number),
                ("1200", 0, payment.amount, invoice.number)]
        description = f"دریافت وجه {invoice.number}"
    else:
        rows = [("2100", payment.amount, 0, invoice.number),
                ("1100", 0, payment.amount, payment.reference or invoice.number)]
        description = f"پرداخت وجه {invoice.number}"
    return post_journal(
        source_type="payment", source_id=payment.pk, source_label=payment.reference or invoice.number,
        description=description, posted_at=payment.paid_at, rows=rows, actor=actor,
    )


def post_fulfillment_batch(batch, actor=None):
    amount = sum((row.quantity * row.unit_cost for row in batch.lines.all()), Decimal(0))
    if amount <= 0:
        return None
    debit, credit = ("5100", "1300") if batch.order.kind == Order.SALES else ("1300", "2200")
    return post_journal(source_type="fulfillment_batch", source_id=batch.pk,
        source_label=batch.number, description=f"گردش نوبت {batch.number} / {batch.order.number}",
        posted_at=batch.created_at,
        rows=[(debit, amount, 0, batch.number), (credit, 0, amount, batch.number)], actor=actor)


def post_invoice_charge(charge, actor=None):
    debit, credit = (("1200", "4100") if charge.invoice.order.kind == Order.SALES
                     else ("2200", "2100"))
    return post_journal(source_type="invoice_charge", source_id=charge.pk,
        source_label=charge.invoice.number, description=f"صورتحساب نوبت {charge.batch.number}",
        posted_at=charge.created_at,
        rows=[(debit, charge.amount, 0, charge.batch.number),
              (credit, 0, charge.amount, charge.batch.number)], actor=actor)


def post_production_batch(batch, actor=None):
    amount = sum((row.quantity * row.unit_cost for row in batch.materials.all()), Decimal(0))
    if amount <= 0:
        return None
    accepted = (amount * batch.accepted_quantity /
                (batch.accepted_quantity + batch.rejected_quantity)).quantize(Decimal(1))
    return post_journal(source_type="production_batch", source_id=batch.pk,
        source_label=batch.number, description=f"تولید و کیفیت {batch.work_order.number}",
        posted_at=batch.created_at,
        rows=[("1400", amount, 0, "مصرف مواد این نوبت"),
              ("1300", 0, amount, "خروج مواد"),
              ("1300", accepted, 0, "محصول قابل قبول"),
              ("5300", amount - accepted, 0, "مردودی کنترل کیفیت"),
              ("1400", 0, amount, "خاتمهٔ نوبت")], actor=actor)


def post_stock_adjustment(movement, actor=None):
    amount = abs(Decimal(movement.change)) * movement.item.purchase_price
    if amount <= 0:
        return None
    if movement.change > 0:
        rows = [("1300", amount, 0, movement.note), ("5200", 0, amount, movement.note)]
    else:
        rows = [("5200", amount, 0, movement.note), ("1300", 0, amount, movement.note)]
    return post_journal(
        source_type="stock_adjustment", source_id=movement.pk,
        source_label=f"ADJ-{movement.pk:05d}", description=f"اصلاح موجودی {movement.item.name}",
        posted_at=movement.created_at, rows=rows, actor=actor,
    )


def post_manufacturing(work_order, actor=None):
    amount = sum((row.total_cost for row in work_order.materials.all()), Decimal("0"))
    if amount <= 0:
        return None
    return post_journal(
        source_type="manufacturing", source_id=work_order.pk,
        source_label=work_order.number,
        description=f"مصرف مواد و رسید تولید {work_order.number}",
        posted_at=work_order.completed_at,
        rows=[
            ("1400", amount, 0, "انتقال مواد به جریان ساخت"),
            ("1300", 0, amount, "مصرف مواد اولیه"),
            ("1300", amount, 0, f"رسید {work_order.product.name}"),
            ("1400", 0, amount, "تکمیل کار در جریان ساخت"),
        ],
        actor=actor,
    )
