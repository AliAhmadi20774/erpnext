from collections import defaultdict
import csv
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

import jdatetime
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Max, Prefetch, Q, Sum
from django.http import Http404, HttpResponse
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST

from .access import (ROLE_FINANCE, ROLE_INVENTORY, ROLE_MANAGER, ROLE_PRODUCTION, ROLE_PURCHASE,
                     ROLE_SALES, has_role,
                     order_access_required, require_order_kind_access, role_required)
from .forms import (BOMComponentFormSet, BOMCreateForm, BOMDraftForm, CustomerForm,
                    FitGapItemForm, ItemEditForm, ItemForm, ManagementDecisionForm, OrderForm,
                    OrderLineFormSet, PaymentForm, StockAdjustmentForm, SupplierForm,
                    ProductionPlanForm, WorkOrderForm)
from .models import (Account, AuditEvent, BillOfMaterials, BOMComponent, Customer, FitGapItem,
                     Fulfillment, Invoice, Item, JournalEntry, JournalLine, ManagementDecision,
                     Order, OrderLine, Payment, ProductionPlan, ProductionPlanLine, StockMovement,
                     Supplier, WorkOrder)
from .manufacturing import (cancel_work_order, complete_work_order, create_work_order,
                            release_work_order)
from .mrp import close_production_plan, create_production_plan
from .product_structure import build_product_tree, product_tree_metrics, validate_bom_activation
from .reporting import PERIODS, REPORTS, build_report, period_start, selected_period
from .services import (adjust_stock, cancel_order, confirm_order, fulfill_order, issue_invoice,
                       record_audit, record_opening_stock, record_payment)
from .templatetags.demo_extras import jalali_date
from .workspace import build_workspace
from .journey import build_order_journey
from .exceptions import build_exception_alerts
from .forms import OrderDatesForm


def _kind(kind):
    if kind not in (Order.SALES, Order.PURCHASE):
        raise Http404
    return kind


def _paginate(request, queryset):
    page = Paginator(queryset, 10).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


def _status_filter(queryset, value):
    if value == "inactive":
        return queryset.filter(is_active=False)
    if value == "all":
        return queryset
    return queryset.filter(is_active=True)


def _months(count=6):
    today = jdatetime.date.fromgregorian(date=timezone.localdate())
    result = []
    for offset in range(count - 1, -1, -1):
        year = today.year
        month = today.month - offset
        while month <= 0:
            year -= 1
            month += 12
        result.append((year, month))
    return result


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def dashboard(request):
    if not has_role(request.user, ROLE_MANAGER):
        return redirect("demo:workspace")
    period = selected_period(request.GET.get("period"))
    start = period_start(period)
    confirmed_sales = Order.objects.filter(kind=Order.SALES, status=Order.CONFIRMED)
    confirmed_purchases = Order.objects.filter(kind=Order.PURCHASE, status=Order.CONFIRMED)
    if start:
        confirmed_sales = confirmed_sales.filter(confirmed_at__gte=start)
        confirmed_purchases = confirmed_purchases.filter(confirmed_at__gte=start)
    sales_total = OrderLine.objects.filter(order__in=confirmed_sales).aggregate(
        total=Sum(F("quantity") * F("unit_price"))
    )["total"] or Decimal(0)
    purchase_total = OrderLine.objects.filter(order__in=confirmed_purchases).aggregate(
        total=Sum(F("quantity") * F("unit_price"))
    )["total"] or Decimal(0)
    monthly = defaultdict(Decimal)
    for line in OrderLine.objects.filter(order__in=confirmed_sales).select_related("order"):
        confirmed_date = timezone.localtime(line.order.confirmed_at).date()
        jalali = jdatetime.date.fromgregorian(date=confirmed_date)
        monthly[(jalali.year, jalali.month)] += line.total
    months = _months()
    ceiling = max((monthly[key] for key in months), default=Decimal(0)) or Decimal(1)
    month_names = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                   "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
    chart = [{"label": month_names[month - 1], "month_key": f"{year}-{month:02d}",
              "value": monthly[(year, month)],
              "height": max(5, round(monthly[(year, month)] / ceiling * 100)) if monthly[(year, month)] else 0}
             for year, month in months]
    sales_invoices = Invoice.objects.filter(order__kind=Order.SALES).prefetch_related("payments")
    purchase_invoices = Invoice.objects.filter(order__kind=Order.PURCHASE).prefetch_related("payments")
    if start:
        sales_invoices = sales_invoices.filter(issued_at__gte=start)
        purchase_invoices = purchase_invoices.filter(issued_at__gte=start)
    recent_orders = Order.objects.select_related("customer", "supplier", "fulfillment", "invoice").prefetch_related("lines")
    if start:
        recent_orders = recent_orders.filter(created_at__gte=start)
    return render(request, "demo/dashboard.html", {
        "exception_alerts": build_exception_alerts()[:3],
        "sales_total": sales_total,
        "purchase_total": purchase_total,
        "sales_count": confirmed_sales.count(),
        "receivable_total": sum((invoice.balance for invoice in sales_invoices), Decimal("0")),
        "payable_total": sum((invoice.balance for invoice in purchase_invoices), Decimal("0")),
        "customer_count": Customer.objects.count(),
        "low_stock": Item.objects.filter(is_active=True, stock__lte=F("reorder_level")).order_by("stock")[:5],
        "low_stock_count": Item.objects.filter(is_active=True, stock__lte=F("reorder_level")).count(),
        "recent_orders": recent_orders[:6],
        "chart": chart,
        "period": period,
        "periods": PERIODS,
    })


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def workspace(request):
    return render(request, "demo/workspace.html", build_workspace(request.user))


@role_required(ROLE_MANAGER, ROLE_SALES)
def customers(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active")
    sort = request.GET.get("sort", "name")
    rows = _status_filter(Customer.objects.annotate(order_count=Count("orders")), status)
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(city__icontains=query))
    rows, page_query = _paginate(request, rows.order_by({"code": "code", "orders": "-order_count"}.get(sort, "name"), "pk"))
    return render(request, "demo/parties.html", {"rows": rows, "kind": "customer", "query": query,
                                                   "status": status, "sort": sort, "page_query": page_query})


@role_required(ROLE_MANAGER, ROLE_PURCHASE)
def suppliers(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active")
    sort = request.GET.get("sort", "name")
    rows = _status_filter(Supplier.objects.annotate(order_count=Count("orders")), status)
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(city__icontains=query))
    rows, page_query = _paginate(request, rows.order_by({"code": "code", "orders": "-order_count"}.get(sort, "name"), "pk"))
    return render(request, "demo/parties.html", {"rows": rows, "kind": "supplier", "query": query,
                                                   "status": status, "sort": sort, "page_query": page_query})


def _save_record(request, form_class, title, back_url, instance=None):
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            record = form.save(commit=False)
            if isinstance(record, Item) and instance is None:
                opening_stock = record.stock
                record.stock = 0
                record.save()
                record_opening_stock(record.pk, opening_stock)
            else:
                record.save()
            form.save_m2m()
            record_audit(request.user, "master_updated" if instance else "master_created",
                         record, str(record))
        messages.success(request, f"{title} ذخیره شد.")
        return redirect(back_url)
    return render(request, "demo/form.html", {"form": form, "title": title, "back_url": back_url})


@role_required(ROLE_MANAGER, ROLE_SALES)
def customer_new(request):
    return _save_record(request, CustomerForm, "مشتری جدید", reverse("demo:customers"))


@role_required(ROLE_MANAGER, ROLE_SALES)
def customer_detail(request, pk):
    return _party_detail(request, Customer, pk, "customer")


@role_required(ROLE_MANAGER, ROLE_SALES)
def customer_edit(request, pk):
    party = get_object_or_404(Customer, pk=pk)
    return _save_record(request, CustomerForm, "ویرایش مشتری", reverse("demo:customer_detail", args=[pk]), party)


@require_POST
@role_required(ROLE_MANAGER, ROLE_SALES)
def customer_toggle(request, pk):
    return _party_toggle(request, Customer, pk, "customer_detail")


@role_required(ROLE_MANAGER, ROLE_PURCHASE)
def supplier_new(request):
    return _save_record(request, SupplierForm, "تامین‌کننده جدید", reverse("demo:suppliers"))


@role_required(ROLE_MANAGER, ROLE_PURCHASE)
def supplier_detail(request, pk):
    return _party_detail(request, Supplier, pk, "supplier")


@role_required(ROLE_MANAGER, ROLE_PURCHASE)
def supplier_edit(request, pk):
    party = get_object_or_404(Supplier, pk=pk)
    return _save_record(request, SupplierForm, "ویرایش تامین‌کننده", reverse("demo:supplier_detail", args=[pk]), party)


@require_POST
@role_required(ROLE_MANAGER, ROLE_PURCHASE)
def supplier_toggle(request, pk):
    return _party_toggle(request, Supplier, pk, "supplier_detail")


def _party_detail(request, model, pk, kind):
    party = get_object_or_404(model, pk=pk)
    recent_orders = party.orders.prefetch_related("lines")[:8]
    return render(request, "demo/party_detail.html", {"party": party, "kind": kind,
                                                       "recent_orders": recent_orders})


def _party_toggle(request, model, pk, detail_name):
    party = get_object_or_404(model, pk=pk)
    party.is_active = not party.is_active
    party.save(update_fields=["is_active"])
    record_audit(request.user, "master_status_changed", party, str(party), {"is_active": party.is_active})
    messages.success(request, "وضعیت طرف حساب به‌روزرسانی شد؛ سوابق سفارش حفظ شدند.")
    return redirect(f"demo:{detail_name}", pk=pk)


@role_required(ROLE_MANAGER, ROLE_INVENTORY)
def item_new(request):
    return _save_record(request, ItemForm, "کالای جدید", reverse("demo:items"))


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def item_detail(request, pk):
    item = get_object_or_404(Item, pk=pk)
    recent_lines = OrderLine.objects.filter(item=item).select_related("order")
    if not has_role(request.user, ROLE_MANAGER, ROLE_INVENTORY, ROLE_FINANCE, ROLE_PRODUCTION):
        recent_lines = recent_lines.filter(
            order__kind=Order.SALES if has_role(request.user, ROLE_SALES) else Order.PURCHASE
        )
    recent_lines = recent_lines[:8]
    active_bom = item.boms.filter(status=BillOfMaterials.ACTIVE).first()
    used_in = item.used_in_boms.filter(bom__status=BillOfMaterials.ACTIVE).select_related(
        "bom__product")
    return render(request, "demo/item_detail.html", {
        "item": item, "recent_lines": recent_lines, "active_bom": active_bom,
        "used_in": used_in,
    })


@role_required(ROLE_MANAGER, ROLE_INVENTORY)
def item_edit(request, pk):
    item = get_object_or_404(Item, pk=pk)
    return _save_record(request, ItemEditForm, "ویرایش کالا", reverse("demo:item_detail", args=[pk]), item)


@require_POST
@role_required(ROLE_MANAGER, ROLE_INVENTORY)
def item_toggle(request, pk):
    item = get_object_or_404(Item, pk=pk)
    item.is_active = not item.is_active
    item.save(update_fields=["is_active"])
    record_audit(request.user, "master_status_changed", item, str(item), {"is_active": item.is_active})
    messages.success(request, "وضعیت کالا به‌روزرسانی شد؛ سوابق سفارش حفظ شدند.")
    return redirect("demo:item_detail", pk=pk)


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def items(request):
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active")
    sort = request.GET.get("sort", "name")
    category = request.GET.get("category", "")
    rows = _status_filter(Item.objects.all(), status)
    if query:
        rows = rows.filter(Q(name__icontains=query) | Q(sku__icontains=query) | Q(category__icontains=query))
    if category:
        rows = rows.filter(category=category)
    rows, page_query = _paginate(request, rows.order_by({"sku": "sku", "stock": "stock"}.get(sort, "name"), "pk"))
    return render(request, "demo/items.html", {"rows": rows, "query": query, "status": status,
                                                "sort": sort, "category": category, "page_query": page_query,
                                                "categories": Item.objects.order_by("category").values_list("category", flat=True).distinct()})


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def product_tree(request, pk=None):
    products = Item.objects.filter(boms__status=BillOfMaterials.ACTIVE).distinct().order_by("name")
    if pk is None:
        roots = products.exclude(used_in_boms__bom__status=BillOfMaterials.ACTIVE)
        product = roots.first() or products.first()
        if product is None:
            return render(request, "demo/product_tree.html", {"products": products})
    else:
        product = get_object_or_404(products, pk=pk)
    try:
        plan_quantity = Decimal(request.GET.get("quantity", "1"))
        if plan_quantity <= 0 or plan_quantity > 10000:
            raise InvalidOperation
    except (InvalidOperation, TypeError):
        plan_quantity = Decimal("1")
    tree = build_product_tree(product, plan_quantity)
    metrics = product_tree_metrics(tree)
    margin = product.sale_price - tree["unit_cost"]
    margin_percent = margin * 100 / product.sale_price if product.sale_price else 0
    return render(request, "demo/product_tree.html", {
        "products": products,
        "product": product,
        "tree": tree,
        "metrics": metrics,
        "plan_quantity": plan_quantity,
        "plan_revenue": product.sale_price * plan_quantity,
        "plan_margin": margin * plan_quantity,
        "margin": margin,
        "margin_percent": margin_percent,
        "versions": product.boms.select_related("activated_by").order_by("-version"),
    })


def _bom_snapshot(bom):
    return {
        "product_id": bom.product_id,
        "code": bom.code,
        "version": bom.version,
        "output_quantity": str(bom.output_quantity),
        "manufacturing_days": bom.manufacturing_days,
        "labor_cost_per_unit": str(bom.labor_cost_per_unit),
        "overhead_cost_per_unit": str(bom.overhead_cost_per_unit),
        "status": bom.status,
        "notes": bom.notes,
        "components": [
            {
                "item_id": row.item_id,
                "quantity": str(row.quantity),
                "scrap_percent": str(row.scrap_percent),
                "sequence": row.sequence,
                "notes": row.notes,
            }
            for row in bom.components.order_by("sequence", "pk")
        ],
    }


def _next_bom_version(product):
    latest = BillOfMaterials.objects.filter(product=product).aggregate(value=Max("version"))["value"] or 0
    return latest + 1


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def bom_versions(request):
    rows = BillOfMaterials.objects.select_related(
        "product", "created_by", "activated_by").annotate(component_count=Count("components"))
    if not has_role(request.user, ROLE_MANAGER):
        rows = rows.filter(status=BillOfMaterials.ACTIVE)
    return render(request, "demo/bom_versions.html", {"rows": rows})


@role_required(ROLE_MANAGER)
def bom_new(request):
    form = BOMCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            product = Item.objects.select_for_update().get(pk=form.cleaned_data["product"].pk)
            version = _next_bom_version(product)
            bom = form.save(commit=False)
            bom.product = product
            bom.version = version
            bom.code = f"BOM-{product.sku}-V{version}"
            bom.status = BillOfMaterials.DRAFT
            bom.created_by = bom.updated_by = request.user
            bom.save()
            record_audit(request.user, "bom_draft_created", bom, str(bom), {
                "current": _bom_snapshot(bom),
            })
        messages.success(request, "نسخهٔ پیش‌نویس BOM ساخته شد؛ اکنون اجزا را تکمیل کنید.")
        return redirect("demo:bom_edit", pk=bom.pk)
    return render(request, "demo/bom_create.html", {"form": form})


@role_required(ROLE_MANAGER)
def bom_edit(request, pk):
    bom = get_object_or_404(BillOfMaterials.objects.select_related("product"), pk=pk)
    if bom.status != BillOfMaterials.DRAFT:
        messages.error(request, "فقط نسخهٔ پیش‌نویس قابل ویرایش است؛ ابتدا یک نسخهٔ جدید بسازید.")
        return redirect("demo:bom_versions")
    existing = list(bom.components.select_related("item").order_by("sequence", "pk"))
    initial = [{"item": row.item_id, "quantity": row.quantity,
                "scrap_percent": row.scrap_percent, "notes": row.notes} for row in existing]
    form = BOMDraftForm(request.POST or None, instance=bom)
    formset = BOMComponentFormSet(
        request.POST or None, prefix="components", initial=initial,
        form_kwargs={"product": bom.product, "existing_item_ids": [row.item_id for row in existing]},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        rows = [row for row in formset.cleaned_data if row and row.get("item")]
        item_ids = [row["item"].pk for row in rows]
        if not rows:
            formset._non_form_errors = formset.error_class(["حداقل یک جزء برای BOM ثبت کنید."])
        elif len(item_ids) != len(set(item_ids)):
            formset._non_form_errors = formset.error_class(["هر جزء فقط یک‌بار می‌تواند در BOM ثبت شود."])
        else:
            with transaction.atomic():
                locked = BillOfMaterials.objects.select_for_update().get(pk=bom.pk)
                if locked.status != BillOfMaterials.DRAFT:
                    messages.error(request, "وضعیت BOM تغییر کرده است؛ صفحه را دوباره بررسی کنید.")
                    return redirect("demo:bom_versions")
                previous = _bom_snapshot(locked)
                locked.output_quantity = form.cleaned_data["output_quantity"]
                locked.manufacturing_days = form.cleaned_data["manufacturing_days"]
                locked.labor_cost_per_unit = form.cleaned_data["labor_cost_per_unit"]
                locked.overhead_cost_per_unit = form.cleaned_data["overhead_cost_per_unit"]
                locked.notes = form.cleaned_data["notes"]
                locked.updated_by = request.user
                locked.save()
                locked.components.all().delete()
                BOMComponent.objects.bulk_create([
                    BOMComponent(bom=locked, item=row["item"], quantity=row["quantity"],
                                 scrap_percent=row["scrap_percent"],
                                 sequence=(index + 1) * 10, notes=row["notes"])
                    for index, row in enumerate(rows)
                ])
                record_audit(request.user, "bom_draft_saved", locked, str(locked), {
                    "previous": previous, "current": _bom_snapshot(locked),
                })
            messages.success(request, "پیش‌نویس BOM و اجزای آن ذخیره شد.")
            return redirect("demo:bom_edit", pk=bom.pk)
    return render(request, "demo/bom_edit.html", {"bom": bom, "form": form, "formset": formset})


@require_POST
@role_required(ROLE_MANAGER)
def bom_clone(request, pk):
    with transaction.atomic():
        source = get_object_or_404(BillOfMaterials.objects.select_for_update().select_related("product"), pk=pk)
        version = _next_bom_version(source.product)
        clone = BillOfMaterials.objects.create(
            product=source.product, code=f"BOM-{source.product.sku}-V{version}", version=version,
            output_quantity=source.output_quantity, manufacturing_days=source.manufacturing_days,
            labor_cost_per_unit=source.labor_cost_per_unit, overhead_cost_per_unit=source.overhead_cost_per_unit,
            status=BillOfMaterials.DRAFT,
            notes=source.notes, created_by=request.user, updated_by=request.user,
        )
        BOMComponent.objects.bulk_create([
            BOMComponent(bom=clone, item=row.item, quantity=row.quantity,
                         scrap_percent=row.scrap_percent, sequence=row.sequence, notes=row.notes)
            for row in source.components.all()
        ])
        record_audit(request.user, "bom_version_cloned", clone, str(clone), {
            "source_id": source.pk, "current": _bom_snapshot(clone),
        })
    messages.success(request, f"نسخهٔ {version} به‌صورت پیش‌نویس ساخته شد.")
    return redirect("demo:bom_edit", pk=clone.pk)


@require_POST
@role_required(ROLE_MANAGER)
def bom_activate(request, pk):
    try:
        with transaction.atomic():
            bom = get_object_or_404(BillOfMaterials.objects.select_for_update().select_related("product"), pk=pk)
            if bom.status != BillOfMaterials.DRAFT:
                raise ValidationError("فقط نسخهٔ پیش‌نویس قابل فعال‌سازی است.")
            if not bom.components.exists():
                raise ValidationError("BOM بدون جزء قابل فعال‌سازی نیست.")
            validate_bom_activation(bom)
            previous_active = list(BillOfMaterials.objects.select_for_update().filter(
                product=bom.product, status=BillOfMaterials.ACTIVE).exclude(pk=bom.pk))
            for previous in previous_active:
                previous.status = BillOfMaterials.OBSOLETE
                previous.updated_by = request.user
                previous.save(update_fields=["status", "updated_by", "updated_at"])
            bom.status = BillOfMaterials.ACTIVE
            bom.activated_by = request.user
            bom.activated_at = timezone.now()
            bom.updated_by = request.user
            bom.save(update_fields=["status", "activated_by", "activated_at", "updated_by", "updated_at"])
            record_audit(request.user, "bom_activated", bom, str(bom), {
                "replaced_ids": [row.pk for row in previous_active],
                "current": _bom_snapshot(bom),
            })
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("demo:bom_edit", pk=pk)
    messages.success(request, "نسخهٔ BOM فعال شد و نسخهٔ فعال قبلی منسوخ گردید.")
    return redirect("demo:product_tree_detail", pk=bom.product_id)


@role_required(ROLE_MANAGER, ROLE_PRODUCTION, ROLE_FINANCE)
def work_orders(request):
    status = request.GET.get("status", "open")
    rows = WorkOrder.objects.select_related("bom__product", "created_by")
    if status == "open":
        rows = rows.filter(status__in=[WorkOrder.DRAFT, WorkOrder.RELEASED])
    elif status in dict(WorkOrder.STATUSES):
        rows = rows.filter(status=status)
    elif status != "all":
        status = "open"
        rows = rows.filter(status__in=[WorkOrder.DRAFT, WorkOrder.RELEASED])
    return render(request, "demo/work_orders.html", {
        "rows": rows, "status": status,
        "can_execute": has_role(request.user, ROLE_MANAGER, ROLE_PRODUCTION),
    })


@role_required(ROLE_MANAGER, ROLE_PRODUCTION)
def work_order_new(request):
    initial = {"planned_start": timezone.localdate(),
               "due_date": timezone.localdate() + timedelta(days=7)}
    bom_id = request.GET.get("bom", "")
    if bom_id.isdigit():
        initial["bom"] = int(bom_id)
    if request.GET.get("quantity", "").isdigit():
        initial["quantity"] = int(request.GET["quantity"])
    try:
        initial["due_date"] = datetime.strptime(request.GET.get("due", ""), "%Y-%m-%d").date()
    except ValueError:
        pass
    form = WorkOrderForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            work_order = create_work_order(
                bom_id=form.cleaned_data["bom"].pk,
                quantity=form.cleaned_data["quantity"],
                planned_start=form.cleaned_data["planned_start"],
                due_date=form.cleaned_data["due_date"],
                notes=form.cleaned_data["notes"], actor=request.user,
                source_plan_id=request.GET.get("plan") or None,
                source_plan_line_id=request.GET.get("line") or None,
            )
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            messages.success(request, "سفارش ساخت و نیاز مواد آن از نسخهٔ BOM ثبت شد.")
            return redirect("demo:work_order_detail", pk=work_order.pk)
    return render(request, "demo/work_order_form.html", {"form": form})


@role_required(ROLE_MANAGER, ROLE_PRODUCTION, ROLE_FINANCE)
def work_order_detail(request, pk):
    work_order = get_object_or_404(
        WorkOrder.objects.select_related("bom__product", "created_by", "released_by",
                                         "completed_by", "source_plan",
                                         "source_plan_line__item").prefetch_related(
                                             "materials__item"), pk=pk)
    materials = list(work_order.materials.all())
    total_cost = Decimal("0")
    shortage_count = 0
    for row in materials:
        row.available_stock = row.item.stock
        row.shortage = max(0, row.required_quantity - row.item.stock)
        shortage_count += bool(row.shortage)
        total_cost += row.total_cost
    movements = work_order.movements.select_related("item")
    return render(request, "demo/work_order_detail.html", {
        "work_order": work_order, "materials": materials, "total_cost": total_cost,
        "shortage_count": shortage_count, "movements": movements,
        "can_execute": has_role(request.user, ROLE_MANAGER, ROLE_PRODUCTION),
    })


def _work_order_action(request, pk, service, success):
    try:
        service(pk, actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, success)
    return redirect("demo:work_order_detail", pk=pk)


@require_POST
@role_required(ROLE_MANAGER, ROLE_PRODUCTION)
def work_order_release(request, pk):
    return _work_order_action(request, pk, release_work_order,
                              "سفارش ساخت برای اجرا آزاد شد.")


@require_POST
@role_required(ROLE_MANAGER, ROLE_PRODUCTION)
def work_order_complete(request, pk):
    return _work_order_action(
        request, pk, complete_work_order,
        "تولید تکمیل شد؛ مواد مصرف و محصول نهایی به موجودی افزوده شد.")


@require_POST
@role_required(ROLE_MANAGER, ROLE_PRODUCTION)
def work_order_cancel(request, pk):
    return _work_order_action(request, pk, cancel_work_order, "سفارش ساخت لغو شد.")


@role_required(ROLE_MANAGER, ROLE_PRODUCTION, ROLE_PURCHASE)
def production_plans(request):
    status = request.GET.get("status", ProductionPlan.OPEN)
    rows = ProductionPlan.objects.select_related("product", "bom", "created_by").annotate(
        line_count=Count("lines"))
    if status in dict(ProductionPlan.STATUSES):
        rows = rows.filter(status=status)
    elif status != "all":
        status = ProductionPlan.OPEN
        rows = rows.filter(status=status)
    return render(request, "demo/production_plans.html", {
        "rows": rows, "status": status,
        "can_plan": has_role(request.user, ROLE_MANAGER, ROLE_PRODUCTION),
    })


@role_required(ROLE_MANAGER, ROLE_PRODUCTION)
def production_plan_new(request):
    initial = {"demand_quantity": 10, "due_date": timezone.localdate() + timedelta(days=14)}
    product_id = request.GET.get("product", "")
    if product_id.isdigit():
        initial["product"] = int(product_id)
    source_id = request.GET.get("order_line", "")
    if source_id.isdigit():
        source = get_object_or_404(OrderLine, pk=int(source_id), order__kind=Order.SALES)
        initial.update(source_order_line=source.pk, product=source.item_id,
                       demand_quantity=source.quantity, due_date=source.order.due_date or initial["due_date"])
    form = ProductionPlanForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            plan = create_production_plan(
                product_id=form.cleaned_data["product"].pk,
                demand_quantity=form.cleaned_data["demand_quantity"],
                due_date=form.cleaned_data["due_date"], notes=form.cleaned_data["notes"],
                actor=request.user,
                source_order_line_id=(form.cleaned_data["source_order_line"].pk
                                      if form.cleaned_data["source_order_line"] else None),
            )
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            messages.success(request, "برنامهٔ MRP اجرا شد؛ نیازها با موجودی و دریافت‌های باز خالص شدند.")
            return redirect("demo:production_plan_detail", pk=plan.pk)
    return render(request, "demo/production_plan_form.html", {"form": form})


@role_required(ROLE_MANAGER, ROLE_PRODUCTION, ROLE_PURCHASE)
def production_plan_detail(request, pk):
    planning_lines = ProductionPlanLine.objects.select_related("item", "supply_bom").prefetch_related(
        Prefetch("work_orders", queryset=WorkOrder.objects.exclude(
            status=WorkOrder.CANCELLED), to_attr="active_work_orders"),
        Prefetch("purchase_orders", queryset=Order.objects.exclude(
            status=Order.CANCELLED).prefetch_related("lines"),
                 to_attr="active_purchase_orders"),
    )
    plan = get_object_or_404(
        ProductionPlan.objects.select_related("product", "bom", "created_by", "closed_by")
        .prefetch_related(Prefetch("lines", queryset=planning_lines, to_attr="planning_lines"),
                          "work_orders__bom__product", "purchase_orders__supplier"), pk=pk)
    lines = plan.planning_lines
    for line in lines:
        if line.supply_type == ProductionPlanLine.MAKE:
            line.converted_quantity = sum(row.quantity for row in line.active_work_orders)
        elif line.supply_type == ProductionPlanLine.BUY:
            line.converted_quantity = sum(
                order_line.quantity for order in line.active_purchase_orders
                for order_line in order.lines.all())
        else:
            line.converted_quantity = 0
        line.remaining_quantity = max(line.net_requirement - line.converted_quantity, 0)
        if line.net_requirement == 0:
            line.conversion_status = "covered"
            line.conversion_label = "پوشش از موجودی/دریافت"
        elif line.remaining_quantity == 0:
            line.conversion_status = "complete"
            line.conversion_label = "کامل تبدیل‌شده"
        elif line.converted_quantity:
            line.conversion_status = "partial"
            line.conversion_label = "تبدیل ناقص"
        else:
            line.conversion_status = "open"
            line.conversion_label = "در انتظار اقدام"
    shortages = sum(line.remaining_quantity > 0 for line in lines)
    converted_count = sum(line.net_requirement > 0 and line.remaining_quantity == 0
                          for line in lines)
    buy_count = sum(line.supply_type == line.BUY and line.remaining_quantity > 0 for line in lines)
    make_count = sum(line.supply_type == line.MAKE and line.remaining_quantity > 0 for line in lines)
    return render(request, "demo/production_plan_detail.html", {
        "plan": plan, "lines": lines, "shortages": shortages,
        "buy_count": buy_count, "make_count": make_count, "converted_count": converted_count,
        "can_plan": has_role(request.user, ROLE_MANAGER, ROLE_PRODUCTION),
    })


@require_POST
@role_required(ROLE_MANAGER, ROLE_PRODUCTION)
def production_plan_close(request, pk):
    try:
        close_production_plan(pk, actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "برنامهٔ MRP بسته شد؛ اسناد تامین متصل برای ردیابی حفظ شدند.")
    return redirect("demo:production_plan_detail", pk=pk)


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE)
def orders(request, kind):
    kind = _kind(kind)
    require_order_kind_access(request.user, kind, include_inventory=True, include_finance=True)
    query = request.GET.get("q", "").strip()
    period = selected_period(request.GET.get("period"))
    start = period_start(period)
    rows = Order.objects.filter(kind=kind).select_related("customer", "supplier", "fulfillment", "invoice").prefetch_related("lines")
    if start:
        rows = rows.filter(confirmed_at__gte=start)
    if query:
        rows = rows.filter(Q(customer__name__icontains=query) | Q(supplier__name__icontains=query) | Q(notes__icontains=query))
    party = request.GET.get("party", "")
    item = request.GET.get("item", "")
    if party.isdigit():
        rows = rows.filter(customer_id=int(party)) if kind == Order.SALES else rows.filter(supplier_id=int(party))
    if item.isdigit():
        rows = rows.filter(lines__item_id=int(item)).distinct()
    month = request.GET.get("month", "")
    try:
        year, number = map(int, month.split("-"))
        first = jdatetime.date(year, number, 1)
        next_month = jdatetime.date(year + (number == 12), number % 12 + 1, 1)
        lower = timezone.make_aware(datetime.combine(first.togregorian(), time.min))
        upper = timezone.make_aware(datetime.combine(next_month.togregorian(), time.min))
        rows = rows.filter(confirmed_at__gte=lower, confirmed_at__lt=upper)
    except (TypeError, ValueError):
        month = ""
    return render(request, "demo/orders.html", {"rows": rows, "kind": kind, "query": query,
                                                 "period": period, "periods": PERIODS,
                                                 "drilldown": bool(party or item or month), "month": month,
                                                 "party": party, "item": item})


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE)
def order_new(request, kind):
    kind = _kind(kind)
    require_order_kind_access(request.user, kind)
    return _order_form(request, kind)


@order_access_required()
def order_edit(request, pk):
    order = get_object_or_404(Order.objects.select_related(
        "customer", "supplier", "source_plan", "source_plan_line__item"), pk=pk)
    if order.status != Order.DRAFT or ProductionPlan.objects.filter(source_order_line__order=order).exists():
        messages.error(request, "فقط پیش‌نویس قابل ویرایش است.")
        return redirect("demo:order_detail", pk=pk)
    return _order_form(request, order.kind, order)


def _order_form(request, kind, order=None):
    existing_lines = list(order.lines.all()) if order else []
    initial = {"party": order.party.pk, "notes": order.notes,
               "due_date": order.due_date, "payment_due_date": order.payment_due_date} if order else None
    initial_lines = [{"item": line.item_id, "quantity": line.quantity} for line in existing_lines]
    suggested_item = None
    suggested_quantity = None
    source_plan = order.source_plan if order else None
    source_plan_line = order.source_plan_line if order else None
    plan_reference_error = None
    if not order and kind == Order.PURCHASE:
        plan_id = request.GET.get("plan", "")
        line_id = request.GET.get("line", "")
        if plan_id or line_id:
            if not (plan_id.isdigit() and line_id.isdigit()):
                plan_reference_error = "ارجاع برنامه و ردیف MRP معتبر نیست."
            else:
                candidate = ProductionPlanLine.objects.select_related("plan", "item").filter(
                    pk=int(line_id), plan_id=int(plan_id)).first()
                if (not candidate or candidate.supply_type != ProductionPlanLine.BUY
                        or candidate.net_requirement <= 0):
                    plan_reference_error = "این ردیف در برنامهٔ MRP پیشنهاد خرید معتبری نیست."
                else:
                    source_plan_line = candidate
                    source_plan = candidate.plan
        if request.method == "GET" and source_plan_line:
            suggested_item = source_plan_line.item
            converted = Order.objects.filter(source_plan_line=source_plan_line).exclude(
                status=Order.CANCELLED).aggregate(total=Sum("lines__quantity"))["total"] or 0
            suggested_quantity = max(source_plan_line.net_requirement - converted, 0)
            initial_lines = [{"item": suggested_item.pk,
                              "quantity": max(1, suggested_quantity)}]
        elif request.method == "GET" and not plan_reference_error:
            item_id = request.GET.get("item", "")
            if item_id.isdigit():
                suggested_item = Item.objects.filter(pk=int(item_id), is_active=True).first()
                if suggested_item:
                    suggested_quantity = max(
                        1, 2 * suggested_item.reorder_level - suggested_item.stock)
                    if request.GET.get("quantity", "").isdigit():
                        suggested_quantity = max(1, int(request.GET["quantity"]))
                    initial_lines = [{"item": suggested_item.pk,
                                      "quantity": suggested_quantity}]
    form = OrderForm(request.POST or None, kind=kind, current_party=order.party if order else None, initial=initial)
    formset = OrderLineFormSet(request.POST or None, prefix="lines",
                               initial=initial_lines,
                               form_kwargs={"existing_item_ids": [line.item_id for line in existing_lines]})
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        lines = [row for row in formset.cleaned_data if row and row.get("item")]
        if not lines:
            formset._non_form_errors = formset.error_class(["حداقل یک کالا به سفارش اضافه کنید."])
        elif plan_reference_error:
            formset._non_form_errors = formset.error_class([plan_reference_error])
        elif source_plan_line and source_plan.status != ProductionPlan.OPEN:
            formset._non_form_errors = formset.error_class([
                "فقط برنامهٔ MRP باز می‌تواند مبنای سفارش خرید باشد."
            ])
        elif source_plan_line and (len(lines) != 1
                                   or lines[0]["item"].pk != source_plan_line.item_id):
            formset._non_form_errors = formset.error_class([
                "سفارش برنامه‌ریزی‌شده باید فقط شامل قلم همان ردیف MRP باشد."
            ])
        else:
            try:
                with transaction.atomic():
                    locked_plan_line = None
                    if source_plan_line:
                        locked_plan_line = ProductionPlanLine.objects.select_for_update().select_related(
                            "plan").get(pk=source_plan_line.pk)
                        if locked_plan_line.plan.status != ProductionPlan.OPEN:
                            raise ValidationError(
                                "فقط برنامهٔ MRP باز می‌تواند مبنای سفارش خرید باشد.")
                        linked_orders = Order.objects.filter(
                            source_plan_line=locked_plan_line).exclude(status=Order.CANCELLED)
                        if order:
                            linked_orders = linked_orders.exclude(pk=order.pk)
                        converted = linked_orders.aggregate(
                            total=Sum("lines__quantity"))["total"] or 0
                        remaining = max(locked_plan_line.net_requirement - converted, 0)
                        if lines[0]["quantity"] > remaining:
                            raise ValidationError(
                                f"مقدار سفارش از باقیماندهٔ پیشنهاد MRP ({remaining}) بیشتر است.")
                    if order:
                        order = Order.objects.select_for_update().get(pk=order.pk)
                        if order.status != Order.DRAFT:
                            messages.error(request, "وضعیت سفارش تغییر کرده است؛ دوباره صفحه را بررسی کنید.")
                            return redirect("demo:order_detail", pk=order.pk)
                        order.lines.all().delete()
                    else:
                        order = Order(kind=kind, source_plan=source_plan,
                                      source_plan_line=locked_plan_line)
                    order.customer = form.cleaned_data["party"] if kind == Order.SALES else None
                    order.supplier = form.cleaned_data["party"] if kind == Order.PURCHASE else None
                    order.notes = form.cleaned_data["notes"]
                    order.due_date = form.cleaned_data["due_date"]
                    if source_plan_line and order.due_date is None:
                        order.due_date = source_plan_line.required_date
                    order.payment_due_date = form.cleaned_data["payment_due_date"]
                    order.save()
                    OrderLine.objects.bulk_create([
                        OrderLine(order=order, item=row["item"], quantity=row["quantity"],
                                  unit_price=row["item"].sale_price if kind == Order.SALES else row["item"].purchase_price)
                        for row in lines
                    ])
                    record_audit(
                        request.user,
                        "order_draft_updated" if existing_lines else "order_draft_created",
                        order, order.number, {
                            "kind": order.kind, "line_count": len(lines),
                            "source_plan_id": source_plan.pk if source_plan else None,
                            "source_plan_line_id": (locked_plan_line.pk
                                                    if locked_plan_line else None),
                        })
            except ValidationError as exc:
                formset._non_form_errors = formset.error_class(exc.messages)
            else:
                messages.success(request, "پیش‌نویس سفارش ذخیره شد.")
                return redirect("demo:order_detail", pk=order.pk)
    elif plan_reference_error:
        formset._non_form_errors = formset.error_class([plan_reference_error])
    existing_ids = [line.item_id for line in existing_lines]
    return render(request, "demo/order_form.html", {"form": form, "formset": formset, "kind": kind,
                                                      "editing": bool(order),
                                                      "suggested_item": suggested_item,
                                                      "suggested_quantity": suggested_quantity,
                                                      "source_plan_line": source_plan_line,
                                                      "catalog": list(Item.objects.filter(Q(is_active=True) | Q(pk__in=existing_ids)).values("id", "sale_price", "purchase_price"))})


@role_required(ROLE_MANAGER, ROLE_PURCHASE)
def purchase_recommendations(request):
    rows = []
    for item in Item.objects.filter(is_active=True, stock__lte=F("reorder_level")).order_by("stock", "name"):
        rows.append({"item": item, "quantity": max(1, 2 * item.reorder_level - item.stock)})
    return render(request, "demo/purchase_recommendations.html", {"rows": rows})


@order_access_required(include_inventory=True, include_finance=True)
def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related(
        "customer", "supplier", "source_plan", "source_plan_line__item").prefetch_related(
            "lines__item"), pk=pk)
    fulfillment = Fulfillment.objects.filter(order=order).first()
    invoice = Invoice.objects.filter(order=order).prefetch_related("payments").first()
    events = [{"label": "ایجاد پیش‌نویس", "at": order.created_at}]
    if order.confirmed_at:
        events.append({"label": "تایید سفارش", "at": order.confirmed_at})
    if fulfillment:
        events.append({"label": "تحویل کالا" if order.kind == Order.SALES else "دریافت کالا", "at": fulfillment.completed_at})
    if invoice:
        events.append({"label": "صدور صورتحساب", "at": invoice.issued_at})
        events.extend({"label": "دریافت وجه" if order.kind == Order.SALES else "پرداخت وجه", "at": payment.paid_at}
                      for payment in invoice.payments.all())
    if order.cancelled_at:
        events.append({"label": "لغو سفارش", "at": order.cancelled_at})
    return render(request, "demo/order_detail.html", {"order": order, "fulfillment": fulfillment,
                                                      "journey": build_order_journey(order, request.user),
                                                      "invoice": invoice, "events": events,
                                                      "can_workflow": has_role(request.user, ROLE_MANAGER,
                                                          ROLE_SALES if order.kind == Order.SALES else ROLE_PURCHASE),
                                                      "can_finance": has_role(request.user, ROLE_MANAGER, ROLE_FINANCE,
                                                          ROLE_SALES if order.kind == Order.SALES else ROLE_PURCHASE),
                                                      "can_fulfill": has_role(request.user, ROLE_MANAGER, ROLE_INVENTORY)})


@require_POST
@order_access_required()
def order_confirm(request, pk):
    order = get_object_or_404(Order, pk=pk)
    try:
        confirm_order(order.pk, actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "سفارش تایید شد. تحویل یا دریافت کالا را جداگانه ثبت کنید.")
    return redirect("demo:order_detail", pk=pk)


@require_POST
@order_access_required(include_inventory=True)
def order_fulfill(request, pk):
    order = get_object_or_404(Order, pk=pk)
    if not has_role(request.user, ROLE_MANAGER, ROLE_INVENTORY):
        raise PermissionDenied
    try:
        fulfill_order(order.pk, actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "تحویل کالا ثبت شد و موجودی انبار به‌روزرسانی شد." if order.kind == Order.SALES
                         else "دریافت کالا ثبت شد و موجودی انبار به‌روزرسانی شد.")
    return redirect("demo:order_detail", pk=pk)


@require_POST
@order_access_required(include_finance=True)
def order_issue_invoice(request, pk):
    order = get_object_or_404(Order, pk=pk)
    try:
        issue_invoice(order.pk, actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "صورتحساب صادر شد.")
    return redirect("demo:order_detail", pk=pk)


@order_access_required(include_finance=True)
def order_payment(request, pk):
    order = get_object_or_404(Order, pk=pk)
    invoice = get_object_or_404(Invoice, order=order)
    form = PaymentForm(request.POST or None, initial={"amount": invoice.balance})
    if request.method == "POST" and form.is_valid():
        try:
            record_payment(invoice.pk, form.cleaned_data["amount"], form.cleaned_data["reference"],
                           actor=request.user, idempotency_key=form.cleaned_data["idempotency_key"])
        except ValidationError as exc:
            form.add_error("amount", " ".join(exc.messages))
        else:
            messages.success(request, "دریافت وجه ثبت شد." if order.kind == Order.SALES else "پرداخت وجه ثبت شد.")
            return redirect("demo:order_detail", pk=pk)
    return render(request, "demo/payment_form.html", {"order": order, "invoice": invoice, "form": form})


@require_POST
@order_access_required()
def order_cancel(request, pk):
    get_object_or_404(Order, pk=pk)
    try:
        cancel_order(pk, actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "سفارش لغو شد.")
    return redirect("demo:order_detail", pk=pk)


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_FINANCE)
def invoice_print(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("order__customer", "order__supplier")
                                .prefetch_related("order__lines__item", "payments"), pk=pk)
    require_order_kind_access(request.user, invoice.order.kind, include_finance=True)
    return render(request, "demo/invoice_print.html", {"invoice": invoice, "order": invoice.order})


@role_required(ROLE_MANAGER, ROLE_INVENTORY)
def inventory(request):
    movements = StockMovement.objects.select_related("item", "order")[:30]
    return render(request, "demo/inventory.html", {
        "items": Item.objects.all(), "movements": movements,
        "total_units": Item.objects.aggregate(total=Sum("stock"))["total"] or 0,
        "low_stock_count": Item.objects.filter(is_active=True, stock__lte=F("reorder_level")).count(),
    })


@role_required(ROLE_MANAGER, ROLE_INVENTORY, ROLE_FINANCE, ROLE_PRODUCTION)
def item_ledger(request, pk):
    item = get_object_or_404(Item, pk=pk)
    rows, page_query = _paginate(request, item.movements.select_related("order"))
    return render(request, "demo/item_ledger.html", {"item": item, "rows": rows, "page_query": page_query})


@role_required(ROLE_MANAGER, ROLE_INVENTORY)
def item_adjust(request, pk):
    item = get_object_or_404(Item, pk=pk)
    form = StockAdjustmentForm(request.POST or None, initial={"new_stock": item.stock})
    if request.method == "POST" and form.is_valid():
        try:
            adjust_stock(item.pk, form.cleaned_data["new_stock"], form.cleaned_data["reason"], actor=request.user)
        except ValidationError as exc:
            form.add_error("new_stock", " ".join(exc.messages))
        else:
            messages.success(request, "موجودی اصلاح شد و دلیل آن در دفتر گردش ثبت شد.")
            return redirect("demo:item_ledger", pk=pk)
    return render(request, "demo/stock_adjust.html", {"item": item, "form": form})


def _safe_csv_text(value):
    value = str(value)
    return "'" + value if value and value[0] in "=+-@\t\r" else value


def _report_csv(report):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="erp-{report["type"]}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    if report["type"] in ("customer_sales", "item_sales", "supplier_purchase"):
        writer.writerow(["نام", "کد", "تعداد سفارش", "تعداد کالا", "مبلغ (تومان)"])
        for row in report["rows"]:
            writer.writerow([_safe_csv_text(row["label"]), _safe_csv_text(row["code"]),
                             row["count"], row["quantity"], row["amount"]])
    elif report["type"] in ("receivables", "payables"):
        writer.writerow(["صورتحساب", "طرف حساب", "تاریخ", "مبلغ", "ثبت‌شده", "مانده"])
        for row in report["rows"]:
            writer.writerow([row["label"], _safe_csv_text(row["party"]), jalali_date(row["date"]),
                             row["amount"], row["paid"], row["balance"]])
    else:
        writer.writerow(["زمان", "کالا", "نوع گردش", "سند یا دلیل", "تغییر", "ماندهٔ قبل", "ماندهٔ بعد"])
        for row in report["rows"]:
            writer.writerow([jalali_date(row["date"]), _safe_csv_text(row["label"]),
                             row["source"], _safe_csv_text(row["note"]), row["change"],
                             row["before"], row["after"]])
    return response


@role_required(ROLE_MANAGER, ROLE_FINANCE)
def reports(request):
    report = build_report(request.GET.get("type"), request.GET.get("period"))
    if request.GET.get("export") == "csv":
        return _report_csv(report)
    return render(request, "demo/reports.html", {"report": report,
                                                  "report_types": REPORTS,
                                                  "periods": PERIODS})


@role_required(ROLE_MANAGER, ROLE_FINANCE)
def accounting_dashboard(request):
    accounts = list(Account.objects.annotate(
        total_debit=Sum("journal_lines__debit"),
        total_credit=Sum("journal_lines__credit"),
    ))
    for account in accounts:
        account.total_debit = account.total_debit or Decimal("0")
        account.total_credit = account.total_credit or Decimal("0")
        if account.account_type in (Account.ASSET, Account.EXPENSE):
            account.balance = account.total_debit - account.total_credit
        else:
            account.balance = account.total_credit - account.total_debit
    totals = JournalLine.objects.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    entries = list(JournalEntry.objects.select_related("posted_by").prefetch_related(
        "lines__account")[:20])
    for entry in entries:
        entry.source_url = _journal_source_url(entry)
    account_by_code = {account.code: account for account in accounts}
    return render(request, "demo/accounting.html", {
        "accounts": accounts,
        "entries": entries,
        "total_debit": totals["debit"] or Decimal("0"),
        "total_credit": totals["credit"] or Decimal("0"),
        "bank_balance": getattr(account_by_code.get("1100"), "balance", Decimal("0")),
        "receivable_balance": getattr(account_by_code.get("1200"), "balance", Decimal("0")),
        "payable_balance": getattr(account_by_code.get("2100"), "balance", Decimal("0")),
        "inventory_balance": getattr(account_by_code.get("1300"), "balance", Decimal("0")),
    })


@role_required(ROLE_MANAGER, ROLE_FINANCE)
def journal_detail(request, pk):
    entry = get_object_or_404(
        JournalEntry.objects.select_related("posted_by").prefetch_related("lines__account"), pk=pk)
    debit = sum((line.debit for line in entry.lines.all()), Decimal("0"))
    credit = sum((line.credit for line in entry.lines.all()), Decimal("0"))
    return render(request, "demo/journal_detail.html", {
        "entry": entry, "debit": debit, "credit": credit, "balanced": debit == credit,
        "source_url": _journal_source_url(entry),
    })


def _journal_source_url(entry):
    if entry.source_type == "manufacturing":
        if WorkOrder.objects.filter(pk=entry.source_id).exists():
            return reverse("demo:work_order_detail", args=[entry.source_id])
    elif entry.source_type in ("opening_stock", "stock_adjustment"):
        movement = StockMovement.objects.filter(pk=entry.source_id).only("item_id").first()
        if movement:
            return reverse("demo:item_ledger", args=[movement.item_id])
    elif entry.source_type == "fulfillment":
        fulfillment = Fulfillment.objects.filter(pk=entry.source_id).only("order_id").first()
        if fulfillment:
            return reverse("demo:order_detail", args=[fulfillment.order_id])
    elif entry.source_type == "invoice":
        invoice = Invoice.objects.filter(pk=entry.source_id).only("order_id").first()
        if invoice:
            return reverse("demo:order_detail", args=[invoice.order_id])
    elif entry.source_type == "payment":
        payment = Payment.objects.filter(pk=entry.source_id).select_related("invoice").first()
        if payment:
            return reverse("demo:order_detail", args=[payment.invoice.order_id])
    return ""


@role_required(ROLE_MANAGER)
def audit_events(request):
    rows, page_query = _paginate(request, AuditEvent.objects.select_related("actor"))
    return render(request, "demo/audit_events.html", {"rows": rows, "page_query": page_query})


@role_required(ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY, ROLE_FINANCE,
               ROLE_PRODUCTION)
def product_scope(request):
    return render(request, "demo/product_scope.html")


@role_required(ROLE_MANAGER)
def management_decisions(request):
    return render(request, "demo/management_decisions.html", {
        "rows": ManagementDecision.objects.select_related("created_by", "updated_by")
        .annotate(gap_count=Count("fit_gap_items"))[:20],
    })


def _decision_audit_snapshot(decision):
    return {
        "meeting_date": decision.meeting_date.isoformat() if decision.meeting_date else None,
        "attendees": decision.attendees,
        "outcome": decision.outcome,
        "architecture": decision.architecture,
        "positives": decision.positives,
        "concerns": decision.concerns,
        "gap_summary": decision.gap_summary,
        "next_step": decision.next_step,
        "owner": decision.owner,
        "due_date": decision.due_date.isoformat() if decision.due_date else None,
        "budget_ceiling": (str(decision.budget_ceiling)
                           if decision.budget_ceiling is not None else None),
    }


@role_required(ROLE_MANAGER)
def management_decision_edit(request, pk=None):
    decision = get_object_or_404(ManagementDecision, pk=pk) if pk else None
    previous = _decision_audit_snapshot(decision) if decision else None
    form = ManagementDecisionForm(request.POST or None, instance=decision)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            record = form.save(commit=False)
            if not record.pk:
                record.created_by = request.user
            record.updated_by = request.user
            record.save()
            record_audit(request.user, "management_decision_saved", record, str(record), {
                "previous": previous,
                "current": _decision_audit_snapshot(record),
            })
        messages.success(request, "نتیجهٔ جلسه و اقدام بعدی ثبت شد.")
        return redirect("demo:management_decisions")
    return render(request, "demo/management_decision_form.html", {
        "form": form, "decision": decision,
    })


def _fit_gap_audit_snapshot(item):
    return {
        "decision_id": item.decision_id,
        "area": item.area,
        "title": item.title,
        "requirement": item.requirement,
        "current_process": item.current_process,
        "evidence": item.evidence,
        "fit": item.fit,
        "solution": item.solution,
        "acceptance_criteria": item.acceptance_criteria,
        "priority": item.priority,
        "effort": item.effort,
        "risk": item.risk,
        "phase": item.phase,
        "cost_low": str(item.cost_low) if item.cost_low is not None else None,
        "cost_high": str(item.cost_high) if item.cost_high is not None else None,
        "owner": item.owner,
        "status": item.status,
    }


def _decision_fit_gap_queryset(decision, request):
    queryset = decision.fit_gap_items.select_related("updated_by")
    area, status = request.GET.get("area", ""), request.GET.get("status", "")
    if area in dict(FitGapItem.AREAS):
        queryset = queryset.filter(area=area)
    if status in dict(FitGapItem.STATUSES):
        queryset = queryset.filter(status=status)
    return queryset, area, status


@role_required(ROLE_MANAGER)
def decision_fit_gap(request, decision_pk):
    decision = get_object_or_404(ManagementDecision, pk=decision_pk)
    queryset, area, status = _decision_fit_gap_queryset(decision, request)
    rows = sorted(queryset, key=lambda item: (-item.priority_score, item.pk))
    complete_count = sum(item.is_complete for item in rows)
    blockers = sum(item.priority == "critical" and item.fit in (FitGapItem.UNKNOWN, FitGapItem.GAP)
                   for item in rows)
    budget = queryset.exclude(status=FitGapItem.DRAFT).aggregate(
        low=Sum("cost_low"), high=Sum("cost_high"))
    return render(request, "demo/decision_fit_gap.html", {
        "decision": decision,
        "rows": rows,
        "area": area,
        "status": status,
        "areas": FitGapItem.AREAS,
        "statuses": FitGapItem.STATUSES,
        "complete_count": complete_count,
        "blockers": blockers,
        "budget_low": budget["low"] or 0,
        "budget_high": budget["high"] or 0,
    })


@role_required(ROLE_MANAGER)
def fit_gap_edit(request, decision_pk, pk=None):
    decision = get_object_or_404(ManagementDecision, pk=decision_pk)
    item = (get_object_or_404(FitGapItem, pk=pk, decision=decision) if pk else None)
    previous = _fit_gap_audit_snapshot(item) if item else None
    form = FitGapItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            record = form.save(commit=False)
            record.decision = decision
            if not record.pk:
                record.created_by = request.user
            record.updated_by = request.user
            record.save()
            record_audit(request.user, "fit_gap_saved", record, str(record), {
                "previous": previous,
                "current": _fit_gap_audit_snapshot(record),
            })
        messages.success(request, "مورد Fit/Gap و برآورد آن ثبت شد.")
        return redirect("demo:decision_fit_gap", decision_pk=decision.pk)
    return render(request, "demo/fit_gap_form.html", {
        "form": form, "decision": decision, "item": item,
    })


@role_required(ROLE_MANAGER)
def decision_fit_gap_csv(request, decision_pk):
    decision = get_object_or_404(ManagementDecision, pk=decision_pk)
    queryset, _, _ = _decision_fit_gap_queryset(decision, request)
    rows = sorted(queryset, key=lambda item: (-item.priority_score, item.pk))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (f'attachment; filename="erp-fit-gap-{decision.pk}.csv"')
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["حوزه", "عنوان", "نیاز واقعی", "فرایند فعلی", "شاهد", "انطباق ERPNext",
                     "راهکار", "معیار پذیرش", "اولویت", "تلاش", "ریسک", "فاز", "حداقل هزینه",
                     "حداکثر هزینه", "مالک", "وضعیت"])
    for item in rows:
        writer.writerow([item.get_area_display(), _safe_csv_text(item.title),
                         _safe_csv_text(item.requirement), _safe_csv_text(item.current_process),
                         _safe_csv_text(item.evidence), item.get_fit_display(),
                         _safe_csv_text(item.solution), _safe_csv_text(item.acceptance_criteria),
                         item.get_priority_display(), item.get_effort_display(),
                         item.get_risk_display(), item.get_phase_display(), item.cost_low or "",
                         item.cost_high or "", _safe_csv_text(item.owner), item.get_status_display()])
    return response


@role_required(ROLE_MANAGER)
def exception_dashboard(request):
    rows = build_exception_alerts()
    owners = sorted({row["owner"] for row in rows})
    area, owner = request.GET.get("area", ""), request.GET.get("owner", "")
    rows = [row for row in rows if (not area or row["area"] == area)
            and (not owner or row["owner"] == owner)]
    return render(request, "demo/exceptions.html", {
        "rows": rows, "area": area, "owner": owner, "owners": owners,
        "areas": [("sales", "فروش"), ("purchase", "خرید"),
                  ("production", "تولید"), ("finance", "مالی")],
    })


@order_access_required(include_finance=True)
def order_dates(request, pk):
    order = get_object_or_404(Order, pk=pk)
    form = OrderDatesForm(request.POST or None, initial={
        "due_date": order.due_date, "payment_due_date": order.payment_due_date})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)
            before = {key: str(getattr(order, key)) for key in ("due_date", "payment_due_date")}
            order.due_date = form.cleaned_data["due_date"]
            order.payment_due_date = form.cleaned_data["payment_due_date"]
            order.save(update_fields=["due_date", "payment_due_date"])
            Invoice.objects.filter(order=order).update(due_date=order.payment_due_date)
            record_audit(request.user, "order_dates_updated", order, order.number,
                         {"before": before, "after": {key: str(getattr(order, key)) for key in before}})
        return redirect("demo:order_detail", pk=pk)
    return render(request, "demo/form.html", {"form": form, "title": "موعدها و سررسید سفارش",
                                             "back_url": reverse("demo:order_detail", args=[pk])})


@role_required(ROLE_MANAGER)
def planning_policy(request):
    from .models import PlanningPolicy
    from .forms import PlanningPolicyForm
    policy = PlanningPolicy.objects.first() or PlanningPolicy()
    form = PlanningPolicyForm(request.POST or None, instance=policy)
    if request.method == "POST" and form.is_valid():
        policy = form.save()
        record_audit(request.user, "planning_policy_updated", policy, "تقویم کاری",
                     {"working_weekdays": policy.working_weekdays, "holidays": policy.holidays})
        return redirect("demo:production_plans")
    return render(request, "demo/form.html", {"form": form, "title": "تقویم کاری برنامه‌ریزی",
                                             "back_url": reverse("demo:production_plans")})


@role_required(ROLE_MANAGER)
def scenario_new(request, pk):
    from .forms import ScenarioForm
    from .scenarios import create_scenario
    plan = get_object_or_404(ProductionPlan, pk=pk)
    form = ScenarioForm(request.POST or None, plan=plan,
                         initial={"quantity": plan.demand_quantity, "label": "سناریوی جایگزین"})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            scenario = create_scenario(plan.pk, label=data["label"], quantity=data["quantity"],
                                       item_id=data["item"].pk if data["item"] else None,
                                       lead_days=data["lead_days"], price=data["price"], actor=request.user)
        except ValidationError as exc:
            form.add_error(None, " ".join(exc.messages))
        else:
            return redirect("demo:scenario_detail", pk=scenario.pk)
    return render(request, "demo/scenario_form.html", {"form": form, "plan": plan})


@role_required(ROLE_MANAGER)
def scenario_detail(request, pk):
    from .models import PlanScenario
    scenario = get_object_or_404(PlanScenario.objects.select_related("plan", "applied_plan"), pk=pk)
    base, result = scenario.baseline, scenario.result
    metrics = []
    for title, key in (("تقاضا", "quantity"), ("اقلام نیازمند خرید", "buy_count"), ("اقلام نیازمند ساخت", "make_count")):
        metrics.append((title, base[key], result[key], result[key] - base[key]))
    before, after = Decimal(base["materials"]["total"]), Decimal(result["materials"]["total"])
    metrics.append(("هزینهٔ مواد برآوردی (تومان)", before, after, after - before))
    if "cost" in base and "cost" in result:
        for title, key in (("دستمزد (تومان)", "labor"), ("سربار (تومان)", "overhead"),
                           ("هزینهٔ کل (تومان)", "total"), ("سود ناخالص برآوردی (تومان)", "gross_profit")):
            before, after = Decimal(base["cost"][key]), Decimal(result["cost"][key])
            metrics.append((title, before, after, after - before))
    lines = {}
    for side, payload in (("baseline", base), ("result", result)):
        for line in payload["lines"]:
            row = lines.setdefault(line["sku"], {"sku": line["sku"], "name": line["name"],
                                                 "baseline": 0, "result": 0})
            row[side] += line["net"]
    for row in lines.values():
        row["difference"] = row["result"] - row["baseline"]
    return render(request, "demo/scenario_detail.html", {
        "scenario": scenario, "metrics": metrics, "lines": list(lines.values()),
        "changed_item": Item.objects.filter(pk=scenario.parameters.get("item_id")).first(),
        "delivery_difference": (datetime.fromisoformat(result["schedule"]["estimated_delivery"])
                                - datetime.fromisoformat(base["schedule"]["estimated_delivery"])).days})


@require_POST
@role_required(ROLE_MANAGER)
def scenario_apply(request, pk):
    from .scenarios import apply_scenario
    try:
        plan = apply_scenario(pk, request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("demo:scenario_detail", pk=pk)
    return redirect("demo:production_plan_detail", pk=plan.pk)


@role_required(ROLE_MANAGER)
def order_cost(request, pk):
    from .costing import create_order_estimate
    order = get_object_or_404(Order, pk=pk, kind=Order.SALES)
    if request.method == "POST":
        try:
            estimate = create_order_estimate(order.pk, request.user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            return redirect(reverse("demo:order_cost", args=[pk]) + f"?estimate={estimate.pk}")
    estimates = order.cost_estimates.all()
    estimate_id = request.GET.get("estimate", "")
    estimate = get_object_or_404(estimates, pk=int(estimate_id)) if estimate_id.isdigit() else estimates.first()
    return render(request, "demo/order_cost.html", {"order": order, "estimate": estimate,
                                                   "estimates": estimates})
