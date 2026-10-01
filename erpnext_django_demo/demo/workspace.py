from django.db.models import F
from django.urls import reverse

from .access import ROLE_INVENTORY, ROLE_MANAGER, ROLE_PURCHASE, ROLE_SALES, has_role
from .models import BillOfMaterials, Customer, Invoice, Item, Order, Supplier


def _outstanding_invoices(kind):
    invoices = Invoice.objects.filter(order__kind=kind).select_related(
        "order__customer", "order__supplier").prefetch_related("payments")
    return [invoice for invoice in invoices if invoice.balance > 0]


def build_workspace(user):
    manager = has_role(user, ROLE_MANAGER)
    sales = manager or has_role(user, ROLE_SALES)
    purchase = manager or has_role(user, ROLE_PURCHASE)
    inventory = manager or has_role(user, ROLE_INVENTORY)

    sales_drafts = Order.objects.filter(kind=Order.SALES, status=Order.DRAFT).select_related("customer")
    purchase_drafts = Order.objects.filter(kind=Order.PURCHASE, status=Order.DRAFT).select_related("supplier")
    pending_fulfillment = Order.objects.filter(
        status=Order.CONFIRMED, fulfillment__isnull=True).select_related("customer", "supplier")
    sales_to_invoice = Order.objects.filter(
        kind=Order.SALES, status=Order.CONFIRMED, fulfillment__isnull=False,
        invoice__isnull=True).select_related("customer")
    purchases_to_invoice = Order.objects.filter(
        kind=Order.PURCHASE, status=Order.CONFIRMED, fulfillment__isnull=False,
        invoice__isnull=True).select_related("supplier")
    receivables = _outstanding_invoices(Order.SALES) if sales else []
    payables = _outstanding_invoices(Order.PURCHASE) if purchase else []
    low_stock = Item.objects.filter(is_active=True, stock__lte=F("reorder_level")).order_by("stock", "name")
    draft_boms = BillOfMaterials.objects.filter(status=BillOfMaterials.DRAFT).select_related("product")
    active_bom_count = BillOfMaterials.objects.filter(status=BillOfMaterials.ACTIVE).count()

    modules = []
    if sales:
        modules.append({
            "key": "sales", "icon": "↗", "title": "فروش تا دریافت",
            "description": "مشتری، سفارش، تحویل، صورتحساب و دریافت وجه",
            "metric": sales_drafts.count() + len(receivables), "metric_label": "اقدام باز",
            "steps": "مشتری ← سفارش ← تحویل ← صورتحساب ← دریافت",
            "actions": [
                ("سفارش فروش جدید", reverse("demo:order_new", args=[Order.SALES]), "primary"),
                ("سفارش‌های فروش", reverse("demo:orders", args=[Order.SALES]), "secondary"),
                ("مشتریان", reverse("demo:customers"), "secondary"),
            ],
        })
    if purchase:
        modules.append({
            "key": "purchase", "icon": "↙", "title": "خرید تا پرداخت",
            "description": "تامین‌کننده، سفارش، دریافت، صورتحساب و پرداخت",
            "metric": purchase_drafts.count() + len(payables), "metric_label": "اقدام باز",
            "steps": "تامین‌کننده ← سفارش ← دریافت ← صورتحساب ← پرداخت",
            "actions": [
                ("سفارش خرید جدید", reverse("demo:order_new", args=[Order.PURCHASE]), "primary"),
                ("پیشنهادهای تامین", reverse("demo:purchase_recommendations"), "secondary"),
                ("تامین‌کنندگان", reverse("demo:suppliers"), "secondary"),
            ],
        })
    if inventory:
        modules.append({
            "key": "inventory", "icon": "▤", "title": "انبار و موجودی",
            "description": "تحویل، دریافت، اصلاح موجودی و دفتر گردش",
            "metric": pending_fulfillment.count() + low_stock.count(), "metric_label": "نیازمند رسیدگی",
            "steps": "سند تاییدشده ← گردش انبار ← مانده و ردپا",
            "actions": [
                ("عملیات انبار", reverse("demo:inventory"), "primary"),
                ("کالاها", reverse("demo:items"), "secondary"),
            ],
        })
    modules.append({
        "key": "product", "icon": "⌘", "title": "محصول و BOM",
        "description": "کالا، نسخهٔ ساخت، اجزا، ضایعات و بهای مواد",
        "metric": draft_boms.count() if manager else active_bom_count,
        "metric_label": "پیش‌نویس BOM" if manager else "BOM فعال",
        "steps": "کالا ← BOM ← نسخهٔ فعال ← درخت و کمبود",
        "actions": [
            ("درخت محصول", reverse("demo:product_tree"), "primary"),
            ("نسخه‌های BOM", reverse("demo:bom_versions"), "secondary"),
        ] + ([("BOM جدید", reverse("demo:bom_new"), "secondary")] if manager else []),
    })
    if manager:
        modules.append({
            "key": "management", "icon": "▥", "title": "مدیریت و کنترل",
            "description": "شاخص‌ها، گزارش‌ها، ممیزی و تصمیم‌های اجرایی",
            "metric": low_stock.count(), "metric_label": "هشدار موجودی",
            "steps": "شاخص ← گزارش ← سند ← تصمیم و ممیزی",
            "actions": [
                ("داشبورد مدیر", reverse("demo:dashboard"), "primary"),
                ("گزارش‌ها", reverse("demo:reports"), "secondary"),
                ("دفتر ممیزی", reverse("demo:audit_events"), "secondary"),
            ],
        })

    tasks = []

    def add_task(area, title, meta, url, urgency="normal"):
        tasks.append({"area": area, "title": title, "meta": meta, "url": url,
                      "urgency": urgency})

    if sales:
        for order in sales_drafts[:3]:
            add_task("فروش", f"تکمیل {order.number}", f"پیش‌نویس · {order.party.name}",
                     reverse("demo:order_detail", args=[order.pk]))
        for order in sales_to_invoice[:3]:
            add_task("فروش", f"صدور صورتحساب {order.number}", order.party.name,
                     reverse("demo:order_detail", args=[order.pk]), "high")
        for invoice in receivables[:3]:
            add_task("دریافتنی", f"پیگیری ماندهٔ {invoice.number}",
                     f"{invoice.order.party.name} · {invoice.balance:,.0f} تومان",
                     reverse("demo:order_detail", args=[invoice.order_id]), "high")
    if purchase:
        for order in purchase_drafts[:3]:
            add_task("خرید", f"تکمیل {order.number}", f"پیش‌نویس · {order.party.name}",
                     reverse("demo:order_detail", args=[order.pk]))
        for order in purchases_to_invoice[:3]:
            add_task("خرید", f"ثبت صورتحساب {order.number}", order.party.name,
                     reverse("demo:order_detail", args=[order.pk]), "high")
        for invoice in payables[:3]:
            add_task("پرداختنی", f"پیگیری ماندهٔ {invoice.number}",
                     f"{invoice.order.party.name} · {invoice.balance:,.0f} تومان",
                     reverse("demo:order_detail", args=[invoice.order_id]), "high")
    if inventory:
        for order in pending_fulfillment[:4]:
            action = "تحویل سفارش فروش" if order.kind == Order.SALES else "دریافت سفارش خرید"
            add_task("انبار", f"{action} {order.number}", order.party.name,
                     reverse("demo:order_detail", args=[order.pk]), "high")
        for item in low_stock[:3]:
            add_task("موجودی", f"تامین {item.name}",
                     f"موجودی {item.stock} · حد سفارش {item.reorder_level}",
                     reverse("demo:item_detail", args=[item.pk]), "high")
    if manager:
        for bom in draft_boms[:3]:
            add_task("BOM", f"تکمیل و بررسی {bom.code}", bom.product.name,
                     reverse("demo:bom_edit", args=[bom.pk]))

    checks = []
    if sales:
        checks.append(("حداقل یک مشتری", Customer.objects.exists(), reverse("demo:customer_new")))
    if purchase:
        checks.append(("حداقل یک تامین‌کننده", Supplier.objects.exists(), reverse("demo:supplier_new")))
    checks.append(("کاتالوگ کالا", Item.objects.exists(), reverse("demo:item_new") if inventory else reverse("demo:items")))
    checks.append(("BOM فعال", BillOfMaterials.objects.filter(status=BillOfMaterials.ACTIVE).exists(),
                   reverse("demo:product_tree")))
    completed = sum(done for _, done, _ in checks)
    setup_percent = round(completed / len(checks) * 100) if checks else 100
    return {"modules": modules, "tasks": tasks[:14], "checks": checks,
            "setup_percent": setup_percent, "task_count": len(tasks)}
