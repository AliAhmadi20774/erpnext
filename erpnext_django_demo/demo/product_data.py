"""Portable, versioned product-master and BOM data exchange."""

from decimal import Decimal, InvalidOperation
import re

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import BillOfMaterials, BOMComponent, Item
from .product_structure import validate_bom_activation
from .services import record_opening_stock


SCHEMA_VERSION = "1.0"
SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,39}$")


def _reject_unknown(row, allowed, label):
    unknown = sorted(set(row) - set(allowed))
    if unknown:
        raise ValidationError(f"فیلد ناشناخته در {label}: {', '.join(unknown)}")


def _decimal(value, label, *, minimum=None, maximum=None, decimal_places=None):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{label} باید عدد معتبر باشد.")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{label} نمی‌تواند کمتر از {minimum} باشد.")
    if maximum is not None and number > maximum:
        raise ValidationError(f"{label} نمی‌تواند بیشتر از {maximum} باشد.")
    if decimal_places is not None and max(-number.as_tuple().exponent, 0) > decimal_places:
        raise ValidationError(f"{label} حداکثر {decimal_places} رقم اعشار می‌پذیرد.")
    return number


def _integer(value, label, *, minimum=0):
    if isinstance(value, bool):
        raise ValidationError(f"{label} باید عدد صحیح باشد.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} باید عدد صحیح باشد.")
    if str(value).strip() not in {str(number), f"{number}.0"}:
        raise ValidationError(f"{label} باید عدد صحیح باشد.")
    if number < minimum:
        raise ValidationError(f"{label} نمی‌تواند کمتر از {minimum} باشد.")
    return number


def validate_product_structure(payload):
    """Validate syntax, referential integrity, active-version rules and graph cycles."""
    if not isinstance(payload, dict):
        raise ValidationError("ریشهٔ فایل ساختار محصول باید یک object باشد.")
    _reject_unknown(payload, {"schema_version", "dataset", "items", "boms"}, "ریشه")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(f"schema_version باید {SCHEMA_VERSION} باشد.")
    dataset = payload.get("dataset")
    if not isinstance(dataset, dict) or not str(dataset.get("code", "")).strip():
        raise ValidationError("dataset.code الزامی است.")
    _reject_unknown(dataset, {"code", "name", "description"}, "dataset")
    for field in ("name", "description"):
        if not str(dataset.get(field, "")).strip():
            raise ValidationError(f"dataset.{field} الزامی است.")
    items = payload.get("items")
    boms = payload.get("boms")
    if not isinstance(items, list) or not items:
        raise ValidationError("items باید یک آرایهٔ غیرخالی باشد.")
    if not isinstance(boms, list):
        raise ValidationError("boms باید یک آرایه باشد.")

    item_by_sku = {}
    for index, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            raise ValidationError(f"items[{index}] باید object باشد.")
        _reject_unknown(row, {"sku", "name", "category", "unit", "sale_price",
                              "purchase_price", "reorder_level", "opening_stock", "lead_time_days",
                              "is_active"}, f"items[{index}]")
        sku = str(row.get("sku", "")).strip().upper()
        if not SKU_PATTERN.fullmatch(sku):
            raise ValidationError(f"کد کالای items[{index}] معتبر نیست: {sku or 'خالی'}")
        if sku in item_by_sku:
            raise ValidationError(f"کد کالا تکراری است: {sku}")
        for field in ("name", "category", "unit"):
            if not str(row.get(field, "")).strip():
                raise ValidationError(f"items[{index}].{field} الزامی است.")
        _integer(row.get("sale_price", 0), f"items[{index}].sale_price")
        _integer(row.get("purchase_price", 0), f"items[{index}].purchase_price")
        _integer(row.get("lead_time_days", 0), f"items[{index}].lead_time_days")
        if row.get("lead_time_days", 0) > 3650:
            raise ValidationError("زمان تامین از ۳۶۵۰ روز بیشتر است.")
        _integer(row.get("reorder_level", 0), f"items[{index}].reorder_level")
        if "opening_stock" in row:
            _integer(row["opening_stock"], f"items[{index}].opening_stock")
        if "is_active" in row and not isinstance(row["is_active"], bool):
            raise ValidationError(f"items[{index}].is_active باید boolean باشد.")
        item_by_sku[sku] = row

    codes = set()
    versions = set()
    active_products = set()
    active_graph = {}
    for index, row in enumerate(boms, start=1):
        if not isinstance(row, dict):
            raise ValidationError(f"boms[{index}] باید object باشد.")
        _reject_unknown(row, {"code", "product_sku", "version", "output_quantity", "manufacturing_days",
                              "labor_cost_per_unit", "overhead_cost_per_unit",
                              "status", "notes", "components"}, f"boms[{index}]")
        code = str(row.get("code", "")).strip().upper()
        product_sku = str(row.get("product_sku", "")).strip().upper()
        if not code:
            raise ValidationError(f"boms[{index}].code الزامی است.")
        if code in codes:
            raise ValidationError(f"کد BOM تکراری است: {code}")
        if product_sku not in item_by_sku:
            raise ValidationError(f"محصول BOM {code} در items تعریف نشده است: {product_sku}")
        version = _integer(row.get("version"), f"نسخهٔ BOM {code}", minimum=1)
        if (product_sku, version) in versions:
            raise ValidationError(f"نسخهٔ {version} برای محصول {product_sku} تکراری است.")
        _integer(row.get("manufacturing_days", 1), f"مدت ساخت BOM {code}")
        for field in ("labor_cost_per_unit", "overhead_cost_per_unit"):
            _integer(row.get(field, 0), f"{field} در BOM {code}")
            if int(row.get(field, 0)) >= 10 ** 14:
                raise ValidationError("نرخ هزینه حداکثر ۱۴ رقم می‌پذیرد.")
        if row.get("manufacturing_days", 1) > 3650:
            raise ValidationError("مدت ساخت از ۳۶۵۰ روز بیشتر است.")
        _decimal(row.get("output_quantity"), f"مقدار خروجی BOM {code}",
                 minimum=Decimal("0.001"), decimal_places=3)
        status = row.get("status", BillOfMaterials.DRAFT)
        if status not in dict(BillOfMaterials.STATUSES):
            raise ValidationError(f"وضعیت BOM {code} معتبر نیست: {status}")
        components = row.get("components")
        if not isinstance(components, list) or not components:
            raise ValidationError(f"BOM {code} باید حداقل یک جزء داشته باشد.")
        component_skus = set()
        component_sequences = set()
        for component_index, component in enumerate(components, start=1):
            if not isinstance(component, dict):
                raise ValidationError(f"جزء {component_index} از BOM {code} باید object باشد.")
            _reject_unknown(component, {"item_sku", "quantity", "scrap_percent",
                                        "sequence", "notes"},
                            f"جزء {component_index} از BOM {code}")
            item_sku = str(component.get("item_sku", "")).strip().upper()
            if item_sku not in item_by_sku:
                raise ValidationError(f"جزء ناشناخته در BOM {code}: {item_sku}")
            if item_sku == product_sku:
                raise ValidationError(f"محصول {product_sku} نمی‌تواند جزء مستقیم BOM خودش باشد.")
            if item_sku in component_skus:
                raise ValidationError(f"جزء {item_sku} در BOM {code} تکراری است.")
            _decimal(component.get("quantity"), f"مقدار {item_sku} در BOM {code}",
                     minimum=Decimal("0.001"), decimal_places=3)
            _decimal(component.get("scrap_percent", 0),
                     f"ضایعات {item_sku} در BOM {code}", minimum=Decimal("0"),
                     maximum=Decimal("100"), decimal_places=2)
            sequence = _integer(component.get("sequence", component_index * 10),
                                f"ترتیب {item_sku} در BOM {code}")
            if sequence in component_sequences:
                raise ValidationError(f"ترتیب {sequence} در BOM {code} تکراری است.")
            component_skus.add(item_sku)
            component_sequences.add(sequence)
        codes.add(code)
        versions.add((product_sku, version))
        if status == BillOfMaterials.ACTIVE:
            if product_sku in active_products:
                raise ValidationError(f"برای محصول {product_sku} بیش از یک BOM فعال تعریف شده است.")
            active_products.add(product_sku)
            active_graph[product_sku] = component_skus

    def visit(sku, path):
        if sku in path:
            chain = " ← ".join((*path, sku))
            raise ValidationError(f"حلقه در ساختار BOM فعال شناسایی شد: {chain}")
        for child_sku in active_graph.get(sku, ()):
            visit(child_sku, (*path, sku))

    for product_sku in active_graph:
        visit(product_sku, ())
    return payload


def build_product_structure(*, product_sku=None, include_stock=False, dataset_code="product-master"):
    """Build deterministic JSON-serializable master data from the database."""
    selected_items = set()
    selected_boms = []
    selected_bom_ids = set()
    active_by_product = {
        bom.product_id: bom for bom in BillOfMaterials.objects.filter(
            status=BillOfMaterials.ACTIVE).select_related("product").prefetch_related(
                "components__item")
    }

    if product_sku:
        root = Item.objects.filter(sku__iexact=product_sku).first()
        if not root:
            raise ValidationError(f"کالا پیدا نشد: {product_sku}")

        def collect(item, path):
            if item.pk in path:
                raise ValidationError("حلقه در ساختار فعال مانع export شد.")
            selected_items.add(item.pk)
            bom = active_by_product.get(item.pk)
            if not bom:
                return
            if bom.pk not in selected_bom_ids:
                selected_boms.append(bom)
                selected_bom_ids.add(bom.pk)
            for component in bom.components.all():
                collect(component.item, (*path, item.pk))

        collect(root, ())
        items = Item.objects.filter(pk__in=selected_items).order_by("sku")
        boms = sorted(selected_boms, key=lambda row: row.code)
    else:
        items = Item.objects.order_by("sku")
        boms = list(BillOfMaterials.objects.select_related("product").prefetch_related(
            "components__item").order_by("code"))

    item_rows = []
    for item in items:
        row = {
            "sku": item.sku, "name": item.name, "category": item.category,
            "unit": item.unit, "sale_price": int(item.sale_price),
            "purchase_price": int(item.purchase_price),
            "lead_time_days": item.lead_time_days,
            "reorder_level": item.reorder_level, "is_active": item.is_active,
        }
        if include_stock:
            row["opening_stock"] = item.stock
        item_rows.append(row)
    bom_rows = [{
        "code": bom.code, "product_sku": bom.product.sku, "version": bom.version,
        "output_quantity": format(bom.output_quantity, "f"), "status": bom.status,
        "manufacturing_days": bom.manufacturing_days,
        "labor_cost_per_unit": int(bom.labor_cost_per_unit),
        "overhead_cost_per_unit": int(bom.overhead_cost_per_unit),
        "notes": bom.notes,
        "components": [{
            "item_sku": component.item.sku,
            "quantity": format(component.quantity, "f"),
            "scrap_percent": format(component.scrap_percent, "f"),
            "sequence": component.sequence, "notes": component.notes,
        } for component in bom.components.all()],
    } for bom in boms]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset": {
            "code": dataset_code,
            "name": "ساختار محصول و BOM نسخه‌پذیر",
            "description": "دادهٔ مرجع قابل review و بازتولید از Git",
        },
        "items": item_rows,
        "boms": bom_rows,
    }
    return validate_product_structure(payload)


@transaction.atomic
def apply_product_structure(payload, *, replace=False, with_opening_stock=False, dry_run=False):
    """Upsert a validated dataset atomically; dry-run executes and rolls everything back."""
    validate_product_structure(payload)
    summary = {"items_created": 0, "items_updated": 0, "boms_created": 0,
               "boms_updated": 0, "components_created": 0, "components_updated": 0,
               "components_deleted": 0, "opening_stock_recorded": 0,
               "opening_stock_skipped": 0}
    catalog = {}
    for row in payload["items"]:
        sku = row["sku"].strip().upper()
        defaults = {
            "name": row["name"].strip(), "category": row["category"].strip(),
            "unit": row["unit"].strip(), "sale_price": int(row.get("sale_price", 0)),
            "purchase_price": int(row.get("purchase_price", 0)),
            "reorder_level": int(row.get("reorder_level", 0)),
            "is_active": row.get("is_active", True),
        }
        item = Item.objects.filter(sku__iexact=sku).first()
        defaults["lead_time_days"] = int(row.get("lead_time_days", item.lead_time_days if item else 0))
        created = item is None
        if created:
            item = Item.objects.create(sku=sku, **defaults)
        else:
            for field, value in defaults.items():
                setattr(item, field, value)
            item.sku = sku
            item.save(update_fields=[*defaults, "sku"])
        summary["items_created" if created else "items_updated"] += 1
        catalog[sku] = item
        if with_opening_stock and "opening_stock" in row and int(row["opening_stock"]) > 0:
            if item.stock == 0 and not item.movements.exists():
                record_opening_stock(item.pk, int(row["opening_stock"]))
                summary["opening_stock_recorded"] += 1
            elif item.stock != int(row["opening_stock"]):
                summary["opening_stock_skipped"] += 1

    imported_boms = []
    for row in payload["boms"]:
        product = catalog[row["product_sku"].strip().upper()]
        code = row["code"].strip().upper()
        version = int(row["version"])
        conflict = BillOfMaterials.objects.filter(product=product, version=version).exclude(
            code=code).first()
        if conflict:
            raise ValidationError(
                f"نسخهٔ {version} محصول {product.sku} قبلاً با کد {conflict.code} ثبت شده است.")
        bom = BillOfMaterials.objects.filter(code__iexact=code).first()
        if bom and (bom.product_id != product.pk or bom.version != version):
            raise ValidationError(f"کد BOM {code} به محصول یا نسخهٔ دیگری تعلق دارد.")
        if not bom:
            bom = BillOfMaterials.objects.create(
                code=code, product=product, version=version,
                output_quantity=Decimal(str(row["output_quantity"])),
                manufacturing_days=int(row.get("manufacturing_days", 1)),
                labor_cost_per_unit=int(row.get("labor_cost_per_unit", 0)),
                overhead_cost_per_unit=int(row.get("overhead_cost_per_unit", 0)),
                status=BillOfMaterials.DRAFT, notes=str(row.get("notes", "")).strip())
            summary["boms_created"] += 1
        else:
            bom.output_quantity = Decimal(str(row["output_quantity"]))
            bom.manufacturing_days = int(row.get("manufacturing_days", bom.manufacturing_days))
            bom.labor_cost_per_unit = int(row.get("labor_cost_per_unit", bom.labor_cost_per_unit))
            bom.overhead_cost_per_unit = int(row.get("overhead_cost_per_unit", bom.overhead_cost_per_unit))
            bom.notes = str(row.get("notes", "")).strip()
            bom.status = BillOfMaterials.DRAFT
            bom.save(update_fields=["output_quantity", "manufacturing_days", "labor_cost_per_unit",
                                    "overhead_cost_per_unit", "notes", "status", "updated_at"])
            summary["boms_updated"] += 1
        component_ids = []
        for component_row in row["components"]:
            component_item = catalog[component_row["item_sku"].strip().upper()]
            component, created = BOMComponent.objects.update_or_create(
                bom=bom, item=component_item, defaults={
                    "quantity": Decimal(str(component_row["quantity"])),
                    "scrap_percent": Decimal(str(component_row.get("scrap_percent", 0))),
                    "sequence": int(component_row.get("sequence", 10)),
                    "notes": str(component_row.get("notes", "")).strip(),
                })
            component_ids.append(component.pk)
            summary["components_created" if created else "components_updated"] += 1
        if replace:
            stale = bom.components.exclude(pk__in=component_ids)
            summary["components_deleted"] += stale.count()
            stale.delete()
        imported_boms.append((bom, row["status"]))

    for bom, desired_status in imported_boms:
        if desired_status == BillOfMaterials.ACTIVE:
            validate_bom_activation(bom)
            BillOfMaterials.objects.filter(
                product=bom.product, status=BillOfMaterials.ACTIVE).exclude(pk=bom.pk).update(
                    status=BillOfMaterials.OBSOLETE)
            bom.status = BillOfMaterials.ACTIVE
            if not bom.activated_at:
                bom.activated_at = timezone.now()
            bom.save(update_fields=["status", "activated_at", "updated_at"])
        elif desired_status == BillOfMaterials.OBSOLETE:
            bom.status = BillOfMaterials.OBSOLETE
            bom.save(update_fields=["status", "updated_at"])

    if dry_run:
        transaction.set_rollback(True)
    return summary
