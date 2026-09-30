from collections import defaultdict
from decimal import Decimal

import jdatetime
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import Http404
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import CustomerForm, ItemEditForm, ItemForm, OrderForm, OrderLineFormSet, SupplierForm
from .models import Customer, Item, Order, OrderLine, StockMovement, Supplier
from .services import confirm_order


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


def dashboard(request):
    confirmed_sales = Order.objects.filter(kind=Order.SALES, status=Order.CONFIRMED)
    confirmed_purchases = Order.objects.filter(kind=Order.PURCHASE, status=Order.CONFIRMED)
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
    chart = [{"label": month_names[month - 1], "value": monthly[(year, month)],
              "height": max(5, round(monthly[(year, month)] / ceiling * 100)) if monthly[(year, month)] else 0}
             for year, month in months]
    return render(request, "demo/dashboard.html", {
        "sales_total": sales_total,
        "purchase_total": purchase_total,
        "sales_count": confirmed_sales.count(),
        "customer_count": Customer.objects.count(),
        "low_stock": Item.objects.filter(stock__lte=F("reorder_level")).order_by("stock")[:5],
        "low_stock_count": Item.objects.filter(stock__lte=F("reorder_level")).count(),
        "recent_orders": Order.objects.select_related("customer", "supplier").prefetch_related("lines")[:6],
        "chart": chart,
    })


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
        form.save()
        messages.success(request, f"{title} ذخیره شد.")
        return redirect(back_url)
    return render(request, "demo/form.html", {"form": form, "title": title, "back_url": back_url})


def customer_new(request):
    return _save_record(request, CustomerForm, "مشتری جدید", reverse("demo:customers"))


def customer_detail(request, pk):
    return _party_detail(request, Customer, pk, "customer")


def customer_edit(request, pk):
    party = get_object_or_404(Customer, pk=pk)
    return _save_record(request, CustomerForm, "ویرایش مشتری", reverse("demo:customer_detail", args=[pk]), party)


@require_POST
def customer_toggle(request, pk):
    return _party_toggle(request, Customer, pk, "customer_detail")


def supplier_new(request):
    return _save_record(request, SupplierForm, "تامین‌کننده جدید", reverse("demo:suppliers"))


def supplier_detail(request, pk):
    return _party_detail(request, Supplier, pk, "supplier")


def supplier_edit(request, pk):
    party = get_object_or_404(Supplier, pk=pk)
    return _save_record(request, SupplierForm, "ویرایش تامین‌کننده", reverse("demo:supplier_detail", args=[pk]), party)


@require_POST
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
    messages.success(request, "وضعیت طرف حساب به‌روزرسانی شد؛ سوابق سفارش حفظ شدند.")
    return redirect(f"demo:{detail_name}", pk=pk)


def item_new(request):
    return _save_record(request, ItemForm, "کالای جدید", reverse("demo:items"))


def item_detail(request, pk):
    item = get_object_or_404(Item, pk=pk)
    recent_lines = OrderLine.objects.filter(item=item).select_related("order")[:8]
    return render(request, "demo/item_detail.html", {"item": item, "recent_lines": recent_lines})


def item_edit(request, pk):
    item = get_object_or_404(Item, pk=pk)
    return _save_record(request, ItemEditForm, "ویرایش کالا", reverse("demo:item_detail", args=[pk]), item)


@require_POST
def item_toggle(request, pk):
    item = get_object_or_404(Item, pk=pk)
    item.is_active = not item.is_active
    item.save(update_fields=["is_active"])
    messages.success(request, "وضعیت کالا به‌روزرسانی شد؛ سوابق سفارش حفظ شدند.")
    return redirect("demo:item_detail", pk=pk)


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


def orders(request, kind):
    kind = _kind(kind)
    query = request.GET.get("q", "").strip()
    rows = Order.objects.filter(kind=kind).select_related("customer", "supplier").prefetch_related("lines")
    if query:
        rows = rows.filter(Q(customer__name__icontains=query) | Q(supplier__name__icontains=query) | Q(notes__icontains=query))
    return render(request, "demo/orders.html", {"rows": rows, "kind": kind, "query": query})


def order_new(request, kind):
    kind = _kind(kind)
    form = OrderForm(request.POST or None, kind=kind)
    formset = OrderLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        lines = [row for row in formset.cleaned_data if row and row.get("item")]
        if not lines:
            formset._non_form_errors = formset.error_class(["حداقل یک کالا به سفارش اضافه کنید."])
        else:
            with transaction.atomic():
                order = Order.objects.create(
                    kind=kind,
                    customer=form.cleaned_data["party"] if kind == Order.SALES else None,
                    supplier=form.cleaned_data["party"] if kind == Order.PURCHASE else None,
                    notes=form.cleaned_data["notes"],
                )
                OrderLine.objects.bulk_create([
                    OrderLine(order=order, item=row["item"], quantity=row["quantity"],
                              unit_price=row["item"].sale_price if kind == Order.SALES else row["item"].purchase_price)
                    for row in lines
                ])
            messages.success(request, "پیش‌نویس سفارش ثبت شد. برای اعمال موجودی، آن را تایید کنید.")
            return redirect("demo:order_detail", pk=order.pk)
    return render(request, "demo/order_form.html", {"form": form, "formset": formset, "kind": kind,
                                                      "catalog": list(Item.objects.filter(is_active=True).values("id", "sale_price", "purchase_price"))})


def order_detail(request, pk):
    order = get_object_or_404(Order.objects.select_related("customer", "supplier").prefetch_related("lines__item"), pk=pk)
    return render(request, "demo/order_detail.html", {"order": order})


@require_POST
def order_confirm(request, pk):
    order = get_object_or_404(Order, pk=pk)
    try:
        confirm_order(order.pk)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, "سفارش تایید شد و موجودی کالا به‌روزرسانی شد.")
    return redirect("demo:order_detail", pk=pk)


def inventory(request):
    movements = StockMovement.objects.select_related("item", "order")[:30]
    return render(request, "demo/inventory.html", {
        "items": Item.objects.all(), "movements": movements,
        "total_units": Item.objects.aggregate(total=Sum("stock"))["total"] or 0,
        "low_stock_count": Item.objects.filter(stock__lte=F("reorder_level")).count(),
    })
