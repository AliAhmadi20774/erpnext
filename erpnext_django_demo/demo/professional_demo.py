"""An additive industrial presentation dataset with explicit engineering inputs."""
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .large_product_demo import large_product_payload
from .models import BillOfMaterials, Customer, Item, Order, OrderLine, Supplier
from .product_data import apply_product_structure
from .services import confirm_order, fulfill_order, issue_invoice, record_audit, record_payment


ROOT = "PX-2400"
TAG = "DEMO-INDUSTRIAL-JOURNEY"
# Illustrative per-part prices in toman, chosen by component, never market quotes.
# Rows follow station/module order in the large BOM (8 stations x 3 modules).
PART_PRICES = (
    (16000000, 450000, 1400000), (8500000, 3200000, 950000), (2200000, 1850000, 180000),
    (820000, 650000, 750000), (1150000, 1600000, 2800000), (1950000, 480000, 2100000),
    (38000000, 4800000, 3200000), (24000000, 2400000, 1800000), (18500000, 4200000, 650000),
    (12500000, 480000, 680000), (12800000, 4600000, 3200000), (6500000, 3200000, 1900000),
    (8500000, 1300000, 1750000), (9500000, 6200000, 2800000), (3400000, 320000, 1900000),
    (850000, 1600000, 2100000), (2600000, 420000, 650000), (38000000, 1450000, 3200000),
    (42500000, 28500000, 2800000), (185000, 35000, 2500), (3200000, 850000, 4200000),
    (68000000, 4500000, 2100000), (650000, 580000, 2900000), (3800000, 1600000, 1850000),
)
PART_QUANTITIES = (
    (1,4,2), (1,1,2), (1,1,2), (6,4,4), (4,2,1), (1,2,1),
    (1,1,1), (1,1,1), (1,1,4), (1,8,8), (1,2,1), (4,4,2),
    (1,2,1), (1,1,1), (2,4,1), (2,1,1), (2,2,2), (1,1,1),
    (1,1,1), (12,24,48), (2,3,1), (1,2,1), (6,3,1), (1,1,1),
)
SPECS = {
    "PX-DRIVE-01": "الکتروموتور گیربکس‌دار ۰٫۷۵ کیلووات، خروجی ۶۰ دور",
    "PX-DRIVE-02": "درایو کنترل دور ۱٫۵ کیلووات، ورودی سه‌فاز",
    "PX-DRIVE-04": "پیچ نصب محرک M8×25، کلاس 8.8",
    "PX-AIR-01": "سیلندر پنوماتیک قطر ۳۲، کورس ۱۰۰ میلی‌متر",
    "PX-AIR-02": "شیر برقی پنوماتیک ۵/۲، بوبین ۲۴ ولت",
    "PX-AIR-04": "شلنگ پنوماتیک پلی‌یورتان قطر ۸ میلی‌متر",
    "PX-S01-M1-P1": "صفحهٔ استیل 304 میز ورودی، ضخامت ۳ میلی‌متر",
    "PX-S02-M1-P1": "پروفیل آلومینیومی ۴۰×۴۰ شاسی نقاله",
    "PX-S02-M2-P1": "زنجیر تخت نقاله استیل، عرض ۸۲٫۵ میلی‌متر",
    "PX-S03-M1-P1": "مخزن واسط استیل 316، حجم ۳۰۰ لیتر",
    "PX-S03-M2-P1": "هد پمپ بهداشتی، دبی اسمی ۲ مترمکعب بر ساعت",
    "PX-S03-M3-P1": "فلومتر بهداشتی DN25، خروجی پالس",
    "PX-S04-M1-P2": "شلنگ سیلیکونی بهداشتی قطر داخلی ۲۰ میلی‌متر",
    "PX-S04-M3-P1": "نازل پرکن استیل 316 با قطع چکه",
    "PX-S06-M1-P1": "رول لیبل ۱۰۰×۷۰ میلی‌متر، ۱۰۰۰ برچسب",
    "PX-S06-M3-P1": "هد چاپ تاریخ جوهرافشان، ارتفاع چاپ ۱۲٫۷ میلی‌متر",
    "PX-S07-M1-P1": "PLC صنعتی ۲۴ ورودی و ۱۶ خروجی، ارتباط Ethernet",
    "PX-S07-M1-P2": "نمایشگر لمسی HMI هفت اینچ، ارتباط Ethernet",
    "PX-S07-M1-P3": "منبع تغذیهٔ ۲۴ ولت DC، جریان ۱۰ آمپر",
    "PX-S07-M2-P1": "کابل کنترل شیلددار ۱۲×۰٫۵ میلی‌متر مربع",
    "PX-S07-M2-P2": "ترمینال ریلی ۲٫۵ میلی‌متر مربع",
    "PX-S07-M2-P3": "وایرشو روکش‌دار ۰٫۵ میلی‌متر مربع",
    "PX-S08-M1-P1": "دوربین صنعتی دو مگاپیکسل، رابط GigE",
}


def professional_payload():
    payload = large_product_payload()
    rename = lambda sku: ROOT if sku == "LINE-500" else sku.replace("LP-", "PX-", 1)
    for item in payload["items"]:
        item["sku"] = rename(item["sku"])
    for bom in payload["boms"]:
        bom["product_sku"] = rename(bom["product_sku"])
        bom["code"] = f"BOM-{bom['product_sku']}-V1"
        bom["notes"] = "مدل PX-2400؛ نرخ‌ها و موجودی برای تمرین ارائه ساخته شده‌اند؛ مبنای مهندسی یا قیمت بازار نیستند."
        for part in bom["components"]:
            part["item_sku"] = rename(part["item_sku"])
    catalog = {item["sku"]: item for item in payload["items"]}
    boms = {bom["product_sku"]: bom for bom in payload["boms"]}
    for sku, item in catalog.items():
        item["name"] = SPECS.get(sku, item["name"])
        if sku in boms:
            item["opening_stock"] = 0
            item["reorder_level"] = 0
            row = boms[sku]
            final, station = sku == ROOT, sku.startswith("PX-S") and "-M" not in sku
            row.update(labor_cost_per_unit=20000000 if final else 3000000 if station else 900000,
                       overhead_cost_per_unit=8000000 if final else 1200000 if station else 350000,
                       manufacturing_days=6 if final else 3 if station else 2)
            item["category"] = "محصول نهایی صنعتی" if final else "زیرمونتاژ صنعتی"
            continue
        electronic = any(word in item["name"] for word in ("حسگر", "PLC", "HMI", "چاپ", "دوربین", "درایو", "منبع تغذیه", "رله"))
        measured = item["unit"] == "متر"
        item.update(category="برق و اتوماسیون" if electronic else "مواد متری" if measured else "قطعات مکانیکی و پنوماتیک",
                    opening_stock=120 if measured else 24 if "حسگر" in item["name"] else 8,
                    reorder_level=20 if measured else 2, lead_time_days=21 if electronic else 10 if measured else 7)
        if "-P" in sku:
            station_number = int(sku[4:6])
            module_number, part_number = int(sku[8]), int(sku[-1])
            item["purchase_price"] = PART_PRICES[(station_number-1)*3+module_number-1][part_number-1]
        item["sale_price"] = item["purchase_price"] * 13 // 10
    catalog["PX-DRIVE-01"].update(opening_stock=0, lead_time_days=30)
    catalog["PX-S07-M1-P1"].update(opening_stock=0, lead_time_days=35)
    catalog["PX-S07-M1-P2"].update(opening_stock=1, lead_time_days=28)
    catalog["PX-S08-M1-P1"].update(opening_stock=1, lead_time_days=28)
    catalog["PX-S04-M3-P1"].update(opening_stock=24)
    catalog["PX-S06-M3-P1"].update(opening_stock=2)
    catalog["PX-S06-M1-P1"]["unit"] = "رول"
    for station in range(1,9):
        for module in range(1,4):
            bom = boms[f"PX-S{station:02d}-M{module}"]
            for index, part in enumerate(bom["components"][:3]):
                quantity = PART_QUANTITIES[(station-1)*3+module-1][index]
                part["quantity"] = str(quantity * int(bom["output_quantity"]))
                part["scrap_percent"] = "2" if catalog[part["item_sku"]]["unit"] == "متر" else "0"

    def full_cost(sku):
        if sku not in boms:
            return Decimal(catalog[sku]["purchase_price"])
        bom = boms[sku]
        materials = sum((full_cost(part["item_sku"]) * Decimal(part["quantity"])
                         * (1+Decimal(part["scrap_percent"])/100) for part in bom["components"]), Decimal(0))
        return materials / Decimal(bom["output_quantity"]) + bom["labor_cost_per_unit"] + bom["overhead_cost_per_unit"]

    for sku in boms:
        cost = int(full_cost(sku).to_integral_value(rounding=ROUND_CEILING))
        catalog[sku].update(purchase_price=cost, sale_price=((cost*5+3)//4+999)//1000*1000)
    catalog[ROOT].update(name="خط خودکار بسته‌بندی مایعات PX-2400", unit="خط")
    payload["dataset"].update(code="industrial-px2400-v1", name=catalog[ROOT]["name"],
                              description="سناریوی صنعتی سفارش دو خط، کمبود PLC، تصمیم تامین و کنترل ماندهٔ وصول؛ دادهٔ ساختگی.")
    return payload


@transaction.atomic
def ensure_professional_demo():
    if Order.objects.filter(notes=TAG).exists():
        return False
    payload = professional_payload()
    identifiers = {row["sku"].lower() for row in payload["items"]}
    codes = {row["code"].lower() for row in payload["boms"]}
    if (any(sku.lower() in identifiers for sku in Item.objects.values_list("sku", flat=True))
            or any(code.lower() in codes for code in BillOfMaterials.objects.values_list("code", flat=True))
            or Customer.objects.filter(code__istartswith="CUS-PX-").exists()
            or Supplier.objects.filter(code__istartswith="SUP-PX-").exists()):
        raise ValidationError("کدهای سناریوی صنعتی با دادهٔ موجود تداخل دارند؛ داده‌ای تغییر نکرد.")
    from .security import ensure_demo_users
    from django.contrib.auth import get_user_model
    from .approvals import request_purchase_approval
    from .costing import create_order_estimate
    from .manufacturing import create_work_order
    from .mrp import create_production_plan
    from .scenarios import create_scenario
    ensure_demo_users()
    actor = get_user_model().objects.get(username="manager")
    apply_product_structure(payload, with_opening_stock=True)
    catalog = Item.objects.in_bulk(field_name="sku")
    customers = [Customer.objects.create(code=f"CUS-PX-{index:02d}", name=name,
                 contact="واحد تامین و مهندسی", city=city) for index, (name, city) in enumerate((
        ("صنایع غذایی سپهر", "قزوین"), ("فرآورده‌های بهداشتی آریا", "کرج"), ("بسته‌بندی مهرگان", "اصفهان")),1)]
    suppliers = [Supplier.objects.create(code=f"SUP-PX-{index:02d}", name=name,
                 contact="واحد فروش تجهیزات", city=city) for index, (name, city) in enumerate((
        ("راهکار کنترل پارس", "تهران"), ("تجهیزات انتقال قدرت البرز", "کرج"), ("قطعات بهداشتی استیل آریا", "تهران")),1)]
    today = timezone.localdate()
    # Two service orders give the same industry completed and partially paid examples.
    for tag, customer, sku, quantity, paid in (
        ("DEMO-INDUSTRIAL-HEALTHY", customers[1], "PX-S04-M3-P1", 4, True),
        ("DEMO-INDUSTRIAL-RECEIVABLE", customers[2], "PX-S06-M3-P1", 1, False),
    ):
        item = catalog[sku]
        order = Order.objects.create(kind=Order.SALES, customer=customer, notes=tag,
                                     due_date=today, payment_due_date=today+timedelta(days=15))
        OrderLine.objects.create(order=order, item=item, quantity=quantity, unit_price=item.sale_price)
        confirm_order(order.pk, actor)
        fulfill_order(order.pk, actor)
        invoice = issue_invoice(order.pk, actor)
        record_payment(invoice.pk, invoice.amount if paid else invoice.amount/2, "PX-SERVICE", actor)
    product = catalog[ROOT]
    due = today + timedelta(days=75)
    order = Order.objects.create(kind=Order.SALES, customer=customers[0], notes=TAG,
                                due_date=due, payment_due_date=due+timedelta(days=30))
    line = OrderLine.objects.create(order=order, item=product, quantity=2, unit_price=product.sale_price)
    confirm_order(order.pk, actor)
    plan = create_production_plan(product_id=product.pk, demand_quantity=2, due_date=due,
                                 source_order_line_id=line.pk, actor=actor,
                                 notes="پروژه توسعهٔ ظرفیت سپهر؛ دو خط PX-2400، کنترل مواد مشترک و موعد تامین PLC.")
    plc = catalog["PX-S07-M1-P1"]
    requirement = plan.lines.get(item=plc)
    purchase = Order.objects.create(kind=Order.PURCHASE, supplier=suppliers[0],
        source_plan=plan, source_plan_line=requirement, notes="DEMO-INDUSTRIAL-PURCHASE",
        due_date=requirement.required_date, payment_due_date=requirement.required_date+timedelta(days=30))
    OrderLine.objects.create(order=purchase, item=plc, quantity=requirement.net_requirement, unit_price=plc.purchase_price)
    request_purchase_approval(purchase.pk, "تامین دو PLC برای تابلو فرمان سفارش سپهر؛ موجودی صفر و زمان تامین ۳۵ روز کاری. جایگزین باید پیش از خرید به تایید مهندسی برسد.", actor)
    root_line = plan.lines.get(parent__isnull=True)
    create_work_order(bom_id=plan.bom_id, quantity=2, planned_start=root_line.release_date or today,
                      due_date=due, source_plan_id=plan.pk,
                      source_plan_line_id=root_line.pk, actor=actor,
                      notes="مونتاژ نهایی دو خط PX-2400 پس از تکمیل زیرمونتاژها و آزمون ایمنی.")
    create_order_estimate(order.pk, actor)
    create_scenario(plan.pk, label="افزایش سفارش سپهر از دو به سه خط", quantity=3, actor=actor)
    create_scenario(plan.pk, label="تاخیر PLC؛ افزایش زمان تامین از ۳۵ به ۵۰ روز کاری",
                    quantity=2, item_id=plc.pk, lead_days=50, actor=actor)
    record_audit(actor, "industrial_demo_seeded", order, order.number,
                 {"dataset":"industrial-px2400-v1", "root_sku":ROOT, "quantity":2})
    return True
