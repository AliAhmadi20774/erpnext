"""Repeatable industrial BOM sample, added without editing existing demo records."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import BillOfMaterials, Item
from .product_data import apply_product_structure
from .services import record_audit


ROOT_SKU = "LINE-500"

# Each station has three engineering modules, each with three purchased parts.
STATIONS = (
    ("تغذیه و مرتب‌سازی بطری", (
        ("میز ورودی", ("صفحهٔ استیل میز", "پایهٔ قابل تنظیم", "راهنمای ورودی بطری")),
        ("گردانندهٔ بطری", ("صفحهٔ گردان", "محور انتقال", "یاتاقان فلنج‌دار")),
        ("جداکنندهٔ بطری", ("گیت جداساز", "حسگر حضور بطری", "براکت حسگر")),
    )),
    ("انتقال و نوار نقاله", (
        ("شاسی نقاله", ("پروفیل آلومینیومی", "ریل جانبی", "پایهٔ شاسی")),
        ("انتقال زنجیری", ("زنجیر تخت نقاله", "چرخ زنجیر", "محور محرک نقاله")),
        ("توقف و هدایت", ("استاپر بطری", "گاید تنظیم عرض", "حسگر ازدحام")),
    )),
    ("آماده‌سازی و اندازه‌گیری مایع", (
        ("مخزن واسط", ("بدنهٔ مخزن استیل", "دریچهٔ بازدید", "حسگر سطح مایع")),
        ("پمپ انتقال", ("هد پمپ بهداشتی", "کوپلینگ پمپ", "آب‌بند مکانیکی")),
        ("اندازه‌گیری دبی", ("فلومتر دیجیتال", "شیر قطع جریان", "اتصال بهداشتی")),
    )),
    ("پرکنی چهارنازله", (
        ("توزیع مایع", ("منیفولد چهارراهه", "شلنگ بهداشتی", "اتصال سریع مایع")),
        ("تنظیم ارتفاع نازل", ("بال‌اسکرو محور عمودی", "راهنمای خطی نازل", "صفحهٔ نگهدارنده")),
        ("تزریق و قطع چکه", ("نازل پرکن", "شیر ضدچکه", "حسگر موقعیت نازل")),
    )),
    ("تغذیه و بستن درب", (
        ("خشاب درب", ("بدنهٔ خشاب", "ریل تغذیهٔ درب", "حسگر وجود درب")),
        ("هد درب‌بند", ("چاک درب‌بند", "محدودکنندهٔ گشتاور", "محور هد")),
        ("نگهدارندهٔ بطری", ("فک نگهدارنده", "پد سیلیکونی فک", "حسگر بسته‌شدن فک")),
    )),
    ("لیبل‌زنی و چاپ تاریخ", (
        ("تغذیهٔ لیبل", ("رول لیبل", "نگهدارندهٔ رول", "حسگر فاصلهٔ لیبل")),
        ("کشندهٔ لیبل", ("غلتک کشنده", "تسمهٔ تایم", "پولی تایم")),
        ("چاپ و اعمال لیبل", ("هد چاپ تاریخ", "براکت اعمال لیبل", "حسگر خواندن تاریخ")),
    )),
    ("کنترل و ایمنی", (
        ("تابلو فرمان", ("PLC صنعتی", "نمایشگر لمسی HMI", "منبع تغذیهٔ ۲۴ ولت")),
        ("دسته‌سیم کنترل", ("کابل کنترل", "ترمینال ریلی", "وایرشو")),
        ("حفاظ و قفل ایمنی", ("قفل درب ایمنی", "شستی توقف اضطراری", "رلهٔ ایمنی")),
    )),
    ("کنترل نهایی و بسته‌بندی", (
        ("بازرسی خروجی", ("دوربین صنعتی", "چراغ بازرسی", "حسگر شمارش محصول")),
        ("انتقال به کارتن", ("غلتک خروجی", "تسمهٔ خروجی", "صفحهٔ انتقال کارتن")),
        ("جداسازی محصول معیوب", ("بازوی انحراف", "سینی محصول معیوب", "حسگر تایید جداسازی")),
    )),
)


def large_product_payload():
    items, boms = [], []
    material_index = 0

    def item(sku, name, *, category="زیرمونتاژ صنعتی", price=0, unit="عدد", opening=0):
        items.append({"sku": sku, "name": name, "category": category, "unit": unit,
                      "purchase_price": price, "sale_price": price * 12 // 10,
                      "opening_stock": opening, "reorder_level": 4,
                      "lead_time_days": 7 if category == "قطعهٔ خریدنی" else 0})
        return sku

    def material(sku, name, price, unit="عدد"):
        nonlocal material_index
        stock = (0, 2, 6, 12, 30)[material_index % 5]
        material_index += 1
        return item(sku, name, category="قطعهٔ خریدنی", price=price, unit=unit, opening=stock)

    def component(sku, quantity=1, scrap=0):
        return {"item_sku": sku, "quantity": str(quantity), "scrap_percent": str(scrap)}

    def bom(sku, components, *, output=1, days=1):
        for sequence, row in enumerate(components, start=1):
            row["sequence"] = sequence * 10
        boms.append({"code": f"BOM-{sku}-V1", "product_sku": sku, "version": 1,
                     "status": BillOfMaterials.ACTIVE, "output_quantity": str(output),
                     "manufacturing_days": days, "labor_cost_per_unit": 150000,
                     "overhead_cost_per_unit": 75000,
                     "notes": "دادهٔ نمایشی خط بسته‌بندی؛ نیازمند تایید مهندسی برای استفادهٔ واقعی.",
                     "components": components})

    drive = item("LP-DRIVE", "مجموعهٔ محرک استاندارد")
    air = item("LP-AIR", "مجموعهٔ پنوماتیک استاندارد")
    for sku, parts in ((drive, (
        ("الکتروموتور گیربکس‌دار", 18000000), ("درایو کنترل دور", 12000000),
        ("کوپلینگ انعطاف‌پذیر", 2400000), ("پیچ نصب محرک", 15000),
    )), (air, (
        ("سیلندر پنوماتیک", 4500000), ("شیر برقی پنوماتیک", 3200000),
        ("واحد مراقبت هوا", 2800000), ("شلنگ پنوماتیک", 85000),
    ))):
        components = []
        for index, (name, price) in enumerate(parts, start=1):
            part = material(f"{sku}-{index:02d}", name, price,
                            unit="متر" if sku == air and index == 4 else "عدد")
            quantity = (8 if sku == drive else 3) if index == 4 else 1
            components.append(component(part, quantity, 3 if sku == air and index == 4 else 0))
        bom(sku, components)

    branches = []
    for station_index, (station_name, modules) in enumerate(STATIONS, start=1):
        station = item(f"LP-S{station_index:02d}", station_name)
        branches.append(component(station))
        module_rows = []
        for module_index, (module_name, part_names) in enumerate(modules, start=1):
            module = item(f"{station}-M{module_index}", module_name)
            module_rows.append(component(module))
            components = []
            # A ten-unit wiring batch demonstrates normalization by BOM output.
            output = 10 if (station_index, module_index) == (7, 2) else 1
            for index, name in enumerate(part_names, start=1):
                measured = name.startswith(("کابل", "شلنگ", "پروفیل", "زنجیر", "تسمه"))
                quantity = Decimal("2.5") if measured else (4 if index == 3 else 1)
                if (station_index, module_index, index) == (4, 3, 1):
                    quantity = Decimal(4)  # Four filling nozzles.
                part = material(f"{module}-P{index}", name,
                                (station_index * 3 + module_index + index) * 125000,
                                unit="متر" if measured else "عدد")
                components.append(component(part, quantity * output, 2 if measured else 0))
            if module_index == 2 and station_index != 7:
                components.append(component(drive, output))
            elif module_index == 3 and station_index != 7:
                components.append(component(air))
            bom(module, components, output=output, days=2)
        bom(station, module_rows, days=3)
    item(ROOT_SKU, "خط خودکار پرکنی و بسته‌بندی", category="محصول نهایی صنعتی", unit="خط")
    bom(ROOT_SKU, branches, days=5)

    # Coherent reference prices for manufacture/stock screens; tree material
    # costs remain the actual recursive sum of purchased parts and scrap.
    by_sku = {row["sku"]: row for row in items}
    by_product = {row["product_sku"]: row for row in boms}

    def cost(sku):
        row = by_product.get(sku)
        if not row:
            return Decimal(by_sku[sku]["purchase_price"])
        return sum((cost(part["item_sku"]) * Decimal(part["quantity"])
                    * (1 + Decimal(part["scrap_percent"]) / 100)
                    for part in row["components"]), Decimal(0)) / Decimal(row["output_quantity"])

    for sku in by_product:
        price = int(cost(sku).to_integral_value())
        by_sku[sku].update(purchase_price=price, sale_price=price * 13 // 10)
    return {"schema_version": "1.0", "dataset": {
        "code": "large-packaging-line", "name": "خط خودکار پرکنی و بسته‌بندی",
        "description": "نمونهٔ پنج‌سطحی با هشت بخش اصلی و قطعات مشترک؛ صرفاً برای ارائهٔ دمو.",
    }, "items": items, "boms": boms}


@transaction.atomic
def ensure_large_product_tree():
    if Item.objects.filter(sku__iexact=ROOT_SKU).exists():
        return False  # Keep user edits, current versions, stock and history.
    payload = large_product_payload()
    skus = {row["sku"].lower() for row in payload["items"]}
    codes = {row["code"].lower() for row in payload["boms"]}
    if (any(sku.lower() in skus for sku in Item.objects.values_list("sku", flat=True))
            or any(code.lower() in codes for code in BillOfMaterials.objects.values_list("code", flat=True))):
        raise ValidationError("کدهای نمونهٔ بزرگ با دادهٔ موجود تداخل دارند؛ هیچ داده‌ای تغییر نکرد.")
    summary = apply_product_structure(payload, with_opening_stock=True)
    root = Item.objects.get(sku=ROOT_SKU)
    record_audit(None, "large_product_tree_seeded", root, str(root), summary)
    return True
