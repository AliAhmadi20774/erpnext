from datetime import date, datetime
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Customer, Fulfillment, Invoice, Item, Order, OrderLine, Payment, StockMovement, Supplier
from .services import cancel_order, confirm_order, fulfill_order, issue_invoice, record_payment
from .templatetags.demo_extras import jalali_date, money


class PersianDisplayTests(TestCase):
    def test_nowruz_boundary_and_money_format(self):
        self.assertEqual(jalali_date(date(2025, 3, 20)), "۱۴۰۳/۱۲/۳۰")
        self.assertEqual(jalali_date(date(2025, 3, 21)), "۱۴۰۴/۰۱/۰۱")
        self.assertEqual(money(1234567), "۱,۲۳۴,۵۶۷")

    def test_order_number_uses_jalali_year(self):
        customer = Customer.objects.create(name="نمونه", code="DATE-1")
        order = Order.objects.create(kind=Order.SALES, customer=customer)
        stamp = timezone.make_aware(datetime(2025, 3, 21, 12))
        Order.objects.filter(pk=order.pk).update(created_at=stamp)
        order.refresh_from_db()
        self.assertTrue(order.number.startswith("SO-1404-"))


class OrderWorkflowTests(TestCase):
    def setUp(self):
        self.item = Item.objects.create(sku="TEST-1", name="کالای تست", category="تست",
                                        sale_price=1000, purchase_price=800, stock=5)
        self.customer = Customer.objects.create(name="مشتری تست", code="C-TEST")
        self.supplier = Supplier.objects.create(name="تامین‌کننده تست", code="S-TEST")

    def test_sale_stock_changes_on_delivery_once(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=3, unit_price=1000)
        confirm_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 5)
        fulfill_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 2)
        self.assertEqual(StockMovement.objects.get(order=order).change, -3)
        with self.assertRaises(ValidationError):
            fulfill_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 2)

    def test_insufficient_stock_keeps_order_draft(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=6, unit_price=1000)
        confirm_order(order.pk)
        with self.assertRaises(ValidationError):
            fulfill_order(order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.CONFIRMED)
        self.assertFalse(StockMovement.objects.filter(order=order).exists())
        self.assertFalse(Fulfillment.objects.filter(order=order).exists())

    def test_purchase_increases_stock(self):
        order = Order.objects.create(kind=Order.PURCHASE, supplier=self.supplier)
        OrderLine.objects.create(order=order, item=self.item, quantity=4, unit_price=800)
        confirm_order(order.pk)
        fulfill_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 9)

    def test_invoice_partial_and_full_payment(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=2, unit_price=1000)
        confirm_order(order.pk)
        with self.assertRaises(ValidationError):
            issue_invoice(order.pk)
        fulfill_order(order.pk)
        invoice = issue_invoice(order.pk)
        self.assertEqual(invoice.amount, 2000)
        record_payment(invoice.pk, 500, "R-1")
        self.assertEqual(invoice.balance, 1500)
        with self.assertRaises(ValidationError):
            record_payment(invoice.pk, 1501)
        record_payment(invoice.pk, 1500)
        self.assertEqual(invoice.balance, 0)
        self.assertEqual(Payment.objects.filter(invoice=invoice).count(), 2)
        self.assertContains(self.client.get(reverse("demo:invoice_print", args=[invoice.pk])), invoice.number)

    def test_cancel_only_before_delivery(self):
        draft = Order.objects.create(kind=Order.SALES, customer=self.customer)
        cancel_order(draft.pk)
        draft.refresh_from_db()
        self.assertEqual(draft.status, Order.CANCELLED)
        with self.assertRaises(ValidationError):
            confirm_order(draft.pk)
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=1, unit_price=1000)
        confirm_order(order.pk)
        fulfill_order(order.pk)
        with self.assertRaises(ValidationError):
            cancel_order(order.pk)

    def test_draft_can_be_edited_before_confirmation(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=1, unit_price=900)
        response = self.client.post(reverse("demo:order_edit", args=[order.pk]), {
            "party": self.customer.pk, "notes": "اصلاح‌شده",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "1",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "20",
            "lines-0-item": self.item.pk, "lines-0-quantity": "2",
        })
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.notes, "اصلاح‌شده")
        self.assertEqual(order.lines.get().quantity, 2)
        self.assertEqual(order.lines.get().unit_price, self.item.sale_price)

    def test_pages_and_create_order(self):
        for name in ("dashboard", "customers", "suppliers", "items", "inventory"):
            self.assertEqual(self.client.get(reverse(f"demo:{name}")).status_code, 200)
        response = self.client.post(reverse("demo:order_new", args=["sales"]), {
            "party": self.customer.pk,
            "notes": "نمونه",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "20",
            "lines-0-item": self.item.pk, "lines-0-quantity": "2",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(OrderLine.objects.get().unit_price, 1000)


class MasterDataTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(name="شرکت نمونه", code="C-001")
        self.item = Item.objects.create(sku="IT-001", name="مانیتور", category="رایانه",
                                        sale_price=1000, purchase_price=800, stock=12)

    def test_case_insensitive_duplicate_code_is_rejected(self):
        response = self.client.post(reverse("demo:customer_new"), {"name": "تکراری", "code": "c-001"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertIn("code", response.context["form"].errors)

    def test_inactive_customer_retains_history_and_cannot_be_selected(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        response = self.client.post(reverse("demo:customer_toggle", args=[self.customer.pk]))
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)
        self.assertEqual(order.customer, self.customer)
        self.assertNotIn(self.customer, self.client.get(reverse("demo:order_new", args=["sales"])).context["form"].fields["party"].queryset)
        self.assertContains(self.client.get(reverse("demo:customer_detail", args=[self.customer.pk])), order.number)

    def test_item_edit_preserves_stock(self):
        response = self.client.post(reverse("demo:item_edit", args=[self.item.pk]), {
            "name": "مانیتور جدید", "sku": "it-001", "category": "رایانه", "unit": "عدد",
            "sale_price": 1200, "purchase_price": 800, "reorder_level": 5,
        })
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual((self.item.name, self.item.sku, self.item.stock), ("مانیتور جدید", "IT-001", 12))

    def test_customer_filter_and_pagination(self):
        for index in range(11):
            Customer.objects.create(name=f"مشتری {index}", code=f"C-{index + 2:03}")
        page = self.client.get(reverse("demo:customers"), {"status": "all", "sort": "code", "page": "2"})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["rows"].paginator.count, 12)
        self.assertEqual(len(page.context["rows"]), 2)


class DemoSeedTests(TestCase):
    def test_seed_creates_consistent_workflows_and_is_idempotent(self):
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Order.objects.count(), 15)
        self.assertEqual(Fulfillment.objects.count(), 14)
        self.assertEqual(Invoice.objects.count(), 7)
        self.assertEqual(Payment.objects.count(), 6)
        self.assertFalse(Item.objects.filter(stock__lt=0).exists())
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Order.objects.count(), 15)
