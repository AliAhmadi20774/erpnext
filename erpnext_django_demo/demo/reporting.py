from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from .models import Invoice, Order, OrderLine, StockMovement

PERIODS = [
    ("all", "همهٔ زمان‌ها"),
    ("30", "۳۰ روز اخیر"),
    ("90", "۹۰ روز اخیر"),
    ("365", "۳۶۵ روز اخیر"),
]
REPORTS = {
    "customer_sales": ("فروش به مشتری", "سفارش‌های فروش تاییدشده به تفکیک مشتری"),
    "item_sales": ("فروش به کالا", "مبلغ و تعداد کالاهای سفارش‌های فروش تاییدشده"),
    "supplier_purchase": ("خرید از تامین‌کننده", "سفارش‌های خرید تاییدشده به تفکیک تامین‌کننده"),
    "receivables": ("دریافتنی‌ها", "ماندهٔ صورتحساب‌های فروش صادرشده"),
    "payables": ("پرداختنی‌ها", "ماندهٔ صورتحساب‌های خرید صادرشده"),
    "stock": ("گردش موجودی", "ورود، خروج و اصلاح موجودی به همراه مانده"),
}


def selected_period(raw):
    return raw if raw in dict(PERIODS) else "all"


def period_start(period):
    return timezone.now() - timedelta(days=int(period)) if period != "all" else None


def _order_link(kind, period, **params):
    values = {"period": period, **params}
    return f"{reverse('demo:orders', args=[kind])}?{urlencode(values)}"


def build_report(report_type, period):
    report_type = report_type if report_type in REPORTS else "customer_sales"
    period = selected_period(period)
    title, description = REPORTS[report_type]
    start = period_start(period)
    rows = []
    total = Decimal("0")

    if report_type in ("customer_sales", "item_sales", "supplier_purchase"):
        sales = report_type != "supplier_purchase"
        kind = Order.SALES if sales else Order.PURCHASE
        lines = OrderLine.objects.filter(order__kind=kind, order__status=Order.CONFIRMED)
        if start:
            lines = lines.filter(order__confirmed_at__gte=start)
        lines = lines.select_related("item", "order__customer", "order__supplier")
        grouped = {}
        for line in lines:
            if report_type == "item_sales":
                entity = line.item
                entity_id = entity.pk
                link = _order_link(Order.SALES, period, item=entity_id)
                code = entity.sku
            else:
                entity = line.order.party
                entity_id = entity.pk
                link = _order_link(kind, period, party=entity_id)
                code = entity.code
            bucket = grouped.setdefault(entity_id, {"label": entity.name, "code": code,
                                                    "count_ids": set(), "quantity": 0,
                                                    "amount": Decimal("0"), "url": link})
            bucket["count_ids"].add(line.order_id)
            bucket["quantity"] += line.quantity
            bucket["amount"] += line.total
        rows = sorted(grouped.values(), key=lambda row: (-row["amount"], row["label"]))
        for row in rows:
            row["count"] = len(row.pop("count_ids"))
            total += row["amount"]

    elif report_type in ("receivables", "payables"):
        kind = Order.SALES if report_type == "receivables" else Order.PURCHASE
        invoices = Invoice.objects.filter(order__kind=kind).select_related("order__customer", "order__supplier").prefetch_related("payments")
        if start:
            invoices = invoices.filter(issued_at__gte=start)
        for invoice in invoices.order_by("-issued_at", "-pk"):
            if invoice.balance <= 0:
                continue
            rows.append({"label": invoice.number, "party": invoice.order.party.name,
                         "date": invoice.issued_at, "amount": invoice.amount,
                         "paid": invoice.paid, "balance": invoice.balance,
                         "url": reverse("demo:order_detail", args=[invoice.order_id])})
            total += invoice.balance

    else:
        movements = StockMovement.objects.select_related("item", "order").order_by("-created_at", "-pk")
        if start:
            movements = movements.filter(created_at__gte=start)
        rows = [{"label": movement.item.name, "date": movement.created_at,
                 "source": movement.get_source_display(), "change": movement.change,
                 "before": movement.balance_before, "after": movement.balance_after,
                 "note": movement.order.number if movement.order else movement.note,
                 "url": reverse("demo:item_ledger", args=[movement.item_id])}
                for movement in movements]

    return {"type": report_type, "title": title, "description": description,
            "period": period, "rows": rows, "total": total, "count": len(rows)}
