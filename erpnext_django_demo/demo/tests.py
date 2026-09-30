from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Customer, Item, Order, OrderLine, StockMovement, Supplier
from .services import confirm_order
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

    def test_sale_updates_stock_once(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=3, unit_price=1000)
        confirm_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 2)
        self.assertEqual(StockMovement.objects.get(order=order).change, -3)
        with self.assertRaises(ValidationError):
            confirm_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 2)

    def test_insufficient_stock_keeps_order_draft(self):
        order = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=order, item=self.item, quantity=6, unit_price=1000)
        with self.assertRaises(ValidationError):
            confirm_order(order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.DRAFT)
        self.assertFalse(StockMovement.objects.filter(order=order).exists())

    def test_purchase_increases_stock(self):
        order = Order.objects.create(kind=Order.PURCHASE, supplier=self.supplier)
        OrderLine.objects.create(order=order, item=self.item, quantity=4, unit_price=800)
        confirm_order(order.pk)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 9)

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
