from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .access import ROLE_INVENTORY, ROLE_MANAGER, ROLE_PURCHASE, ROLE_SALES
from .database_backup import create_sqlite_backup, restore_sqlite_backup
from .models import (AuditEvent, Customer, Fulfillment, Invoice, Item, ManagementDecision, Order,
                     OrderLine, Payment, StockMovement, Supplier)
from .services import adjust_stock, cancel_order, confirm_order, fulfill_order, issue_invoice, record_opening_stock, record_payment
from .templatetags.demo_extras import jalali_date, money


class AuthenticatedTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        group = Group.objects.create(name=ROLE_MANAGER)
        cls.manager = get_user_model().objects.create_user("test-manager", password="test-password")
        cls.manager.groups.add(group)

    def _pre_setup(self):
        super()._pre_setup()
        self.client.force_login(self.manager)


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


class OrderWorkflowTests(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
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

    def test_purchase_receipt_invoice_and_payment_views(self):
        order = Order.objects.create(kind=Order.PURCHASE, supplier=self.supplier)
        OrderLine.objects.create(order=order, item=self.item, quantity=2, unit_price=800)
        self.assertEqual(self.client.post(reverse("demo:order_confirm", args=[order.pk])).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 5)
        self.assertEqual(self.client.post(reverse("demo:order_fulfill", args=[order.pk])).status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 7)
        self.client.post(reverse("demo:order_fulfill", args=[order.pk]))
        self.item.refresh_from_db()
        self.assertEqual(self.item.stock, 7)
        self.client.post(reverse("demo:order_issue_invoice", args=[order.pk]))
        invoice = Invoice.objects.get(order=order)
        self.assertEqual(invoice.amount, 1600)
        response = self.client.post(reverse("demo:order_payment", args=[order.pk]),
                                    {"amount": 1600, "reference": "BUY-1", "idempotency_key": uuid.uuid4()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(invoice.balance, 0)

    def test_low_stock_recommendation_prefills_purchase(self):
        response = self.client.get(reverse("demo:purchase_recommendations"))
        self.assertContains(response, self.item.name)
        form_page = self.client.get(reverse("demo:order_new", args=["purchase"]), {"item": self.item.pk})
        first = form_page.context["formset"].forms[0]
        self.assertEqual(first.initial["item"], self.item.pk)
        self.assertEqual(first.initial["quantity"], 15)

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


class MasterDataTests(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
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

    def test_new_item_records_opening_stock(self):
        response = self.client.post(reverse("demo:item_new"), {
            "name": "ماوس", "sku": "it-002", "category": "رایانه", "unit": "عدد",
            "sale_price": 500, "purchase_price": 300, "stock": 7, "reorder_level": 3,
        })
        self.assertEqual(response.status_code, 302)
        item = Item.objects.get(sku="IT-002")
        movement = item.movements.get()
        self.assertEqual((item.stock, movement.source, movement.change, movement.balance_after),
                         (7, StockMovement.OPENING, 7, 7))


class InventoryLedgerTests(AuthenticatedTestCase):
    def test_adjustment_requires_reason_and_keeps_balances(self):
        item = Item.objects.create(sku="LEDGER-1", name="کالای دفتر", category="آزمون", stock=0,
                                   sale_price=100, purchase_price=80)
        record_opening_stock(item.pk, 10)
        with self.assertRaises(ValidationError):
            adjust_stock(item.pk, -1, "اصلاح")
        with self.assertRaises(ValidationError):
            adjust_stock(item.pk, 7, "")
        with self.assertRaises(ValidationError):
            adjust_stock(item.pk, 10, "بدون تغییر")
        adjust_stock(item.pk, 7, "شمارش فیزیکی")
        item.refresh_from_db()
        movements = list(item.movements.order_by("created_at", "pk"))
        self.assertEqual(item.stock, 7)
        self.assertEqual([(m.balance_before, m.change, m.balance_after) for m in movements],
                         [(0, 10, 10), (10, -3, 7)])
        self.assertContains(self.client.get(reverse("demo:item_ledger", args=[item.pk])), "شمارش فیزیکی")


class ReportingTests(AuthenticatedTestCase):
    def test_kpis_reports_drilldowns_and_csv_match_source_documents(self):
        customer = Customer.objects.create(name="=DEMO", code="RC-1")
        supplier = Supplier.objects.create(name="Supplier", code="RS-1")
        item = Item.objects.create(sku="RI-1", name="Monitor", category="IT", stock=10,
                                   sale_price=1000, purchase_price=600, reorder_level=12)
        sale = Order.objects.create(kind=Order.SALES, customer=customer)
        OrderLine.objects.create(order=sale, item=item, quantity=2, unit_price=1000)
        confirm_order(sale.pk)
        fulfill_order(sale.pk)
        sale_invoice = issue_invoice(sale.pk)
        record_payment(sale_invoice.pk, 500)
        purchase = Order.objects.create(kind=Order.PURCHASE, supplier=supplier)
        OrderLine.objects.create(order=purchase, item=item, quantity=3, unit_price=600)
        confirm_order(purchase.pk)
        fulfill_order(purchase.pk)
        purchase_invoice = issue_invoice(purchase.pk)
        record_payment(purchase_invoice.pk, 300)

        dashboard = self.client.get(reverse("demo:dashboard"))
        self.assertEqual(dashboard.context["sales_total"], 2000)
        self.assertEqual(dashboard.context["purchase_total"], 1800)
        self.assertEqual(dashboard.context["receivable_total"], 1500)
        self.assertEqual(dashboard.context["payable_total"], 1500)
        self.assertEqual(dashboard.context["low_stock_count"], 1)
        for report_type, expected in (("customer_sales", 2000), ("item_sales", 2000),
                                      ("supplier_purchase", 1800), ("receivables", 1500),
                                      ("payables", 1500)):
            report = self.client.get(reverse("demo:reports"), {"type": report_type}).context["report"]
            self.assertEqual(report["total"], expected)
            self.assertEqual(report["count"], 1)
            self.assertTrue(report["rows"][0]["url"])
        stock = self.client.get(reverse("demo:reports"), {"type": "stock"}).context["report"]
        self.assertEqual(stock["count"], 2)
        self.assertEqual([row["change"] for row in stock["rows"]], [3, -2])
        drilldown = self.client.get(reverse("demo:orders", args=["sales"]), {"party": customer.pk})
        self.assertContains(drilldown, sale.number)
        csv_response = self.client.get(reverse("demo:reports"), {"type": "customer_sales", "export": "csv"})
        self.assertTrue(csv_response.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"'=DEMO", csv_response.content)

        old_sale = Order.objects.create(kind=Order.SALES, customer=customer)
        OrderLine.objects.create(order=old_sale, item=item, quantity=1, unit_price=1000)
        confirm_order(old_sale.pk)
        Order.objects.filter(pk=old_sale.pk).update(confirmed_at=timezone.now() - timedelta(days=40))
        dashboard_30 = self.client.get(reverse("demo:dashboard"), {"period": "30"})
        self.assertEqual(dashboard_30.context["sales_total"], 2000)
        self.assertEqual(dashboard_30.context["sales_count"], 1)
        report_30 = self.client.get(reverse("demo:reports"),
                                    {"type": "customer_sales", "period": "30"}).context["report"]
        self.assertEqual(report_30["total"], 2000)


class DemoSeedTests(TestCase):
    def test_seed_creates_consistent_workflows_and_is_idempotent(self):
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Order.objects.count(), 15)
        decision = ManagementDecision.objects.create(outcome=ManagementDecision.PENDING)
        AuditEvent.objects.create(action="management_decision_saved",
                                  object_type=ManagementDecision._meta.model_name,
                                  object_id=str(decision.pk), object_label=str(decision))
        Customer.objects.create(name="دادهٔ تمرینی", code="TEMP-C")
        call_command("reset_demo", "--yes", "--no-backup", stdout=StringIO())
        self.assertEqual((Customer.objects.count(), Supplier.objects.count(), Item.objects.count(), Order.objects.count()),
                         (6, 3, 10, 15))
        self.assertEqual(Fulfillment.objects.count(), 14)
        self.assertEqual(Invoice.objects.count(), 10)
        self.assertEqual(Payment.objects.count(), 9)
        self.assertTrue(ManagementDecision.objects.filter(pk=decision.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(object_type=ManagementDecision._meta.model_name,
                                                  object_id=str(decision.pk)).exists())
        self.assertFalse(Item.objects.filter(stock__lt=0).exists())
        for item in Item.objects.all():
            balance = 0
            for movement in item.movements.order_by("created_at", "pk"):
                self.assertEqual(movement.balance_before, balance)
                balance += movement.change
                self.assertEqual(movement.balance_after, balance)
            self.assertEqual(balance, item.stock)
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Order.objects.count(), 15)


class AccessAuditAndRecoveryTests(TestCase):
    def setUp(self):
        self.users = {}
        for role in (ROLE_MANAGER, ROLE_SALES, ROLE_PURCHASE, ROLE_INVENTORY):
            group = Group.objects.create(name=role)
            user = get_user_model().objects.create_user(role, password="test-password")
            user.groups.add(group)
            self.users[role] = user
        self.customer = Customer.objects.create(name="مشتری", code="SEC-C")
        self.supplier = Supplier.objects.create(name="تامین", code="SEC-S")
        self.item = Item.objects.create(name="کالا", sku="SEC-I", category="تست", stock=10,
                                        sale_price=1000, purchase_price=700)
        self.sale = Order.objects.create(kind=Order.SALES, customer=self.customer)
        OrderLine.objects.create(order=self.sale, item=self.item, quantity=2, unit_price=1000)
        self.purchase = Order.objects.create(kind=Order.PURCHASE, supplier=self.supplier)
        OrderLine.objects.create(order=self.purchase, item=self.item, quantity=2, unit_price=700)

    def login(self, role):
        self.client.force_login(self.users[role])

    def test_login_and_least_privilege_roles(self):
        self.assertRedirects(self.client.get(reverse("demo:customers")),
                             f"{reverse('login')}?next={reverse('demo:customers')}")
        self.login(ROLE_SALES)
        self.assertContains(self.client.get(reverse("demo:product_scope")), "این نسخه چه هست")
        self.assertEqual(self.client.get(reverse("demo:orders", args=["sales"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("demo:orders", args=["purchase"])).status_code, 403)
        self.assertEqual(self.client.get(reverse("demo:suppliers")).status_code, 403)
        self.assertEqual(self.client.get(reverse("demo:inventory")).status_code, 403)
        items = self.client.get(reverse("demo:items"))
        self.assertContains(items, "قیمت فروش")
        self.assertNotContains(items, "قیمت خرید")

        self.login(ROLE_PURCHASE)
        self.assertEqual(self.client.get(reverse("demo:orders", args=["purchase"])).status_code, 200)
        self.assertEqual(self.client.get(reverse("demo:customers")).status_code, 403)
        self.assertEqual(self.client.get(reverse("demo:reports")).status_code, 403)

        self.login(ROLE_INVENTORY)
        self.assertEqual(self.client.get(reverse("demo:inventory")).status_code, 200)
        self.assertEqual(self.client.post(reverse("demo:order_confirm", args=[self.sale.pk])).status_code, 403)
        order_page = self.client.get(reverse("demo:order_detail", args=[self.sale.pk]))
        self.assertNotContains(order_page, "قیمت واحد")
        self.assertNotContains(order_page, "مبلغ کل سفارش")

    def test_sensitive_actions_are_audited_with_actor(self):
        self.login(ROLE_SALES)
        self.client.post(reverse("demo:order_confirm", args=[self.sale.pk]))
        event = AuditEvent.objects.get(action="order_confirmed")
        self.assertEqual((event.actor, event.object_id), (self.users[ROLE_SALES], str(self.sale.pk)))

        self.login(ROLE_INVENTORY)
        self.client.post(reverse("demo:order_fulfill", args=[self.sale.pk]))
        self.assertTrue(AuditEvent.objects.filter(action="order_fulfilled", actor=self.users[ROLE_INVENTORY]).exists())
        self.client.post(reverse("demo:item_adjust", args=[self.item.pk]),
                         {"new_stock": 7, "reason": "شمارش دوره‌ای"})
        self.assertTrue(AuditEvent.objects.filter(action="stock_adjusted", actor=self.users[ROLE_INVENTORY]).exists())

    def test_payment_request_is_idempotent(self):
        confirm_order(self.sale.pk)
        fulfill_order(self.sale.pk)
        invoice = issue_invoice(self.sale.pk)
        request_key = uuid.uuid4()
        first = record_payment(invoice.pk, 500, "BANK-1", self.users[ROLE_SALES], request_key)
        second = record_payment(invoice.pk, 500, "BANK-1", self.users[ROLE_SALES], request_key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Payment.objects.filter(invoice=invoice).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="payment_recorded").count(), 1)

    def test_csrf_is_required_for_sensitive_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users[ROLE_SALES])
        response = client.post(reverse("demo:order_confirm", args=[self.sale.pk]))
        self.assertEqual(response.status_code, 403)
        self.sale.refresh_from_db()
        self.assertEqual(self.sale.status, Order.DRAFT)

    def test_only_manager_can_record_an_actionable_management_decision(self):
        self.login(ROLE_SALES)
        self.assertEqual(self.client.get(reverse("demo:management_decisions")).status_code, 403)
        self.login(ROLE_MANAGER)
        invalid = self.client.post(reverse("demo:management_decision_new"), {
            "outcome": ManagementDecision.PILOT,
            "architecture": ManagementDecision.UNDECIDED,
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(ManagementDecision.objects.exists())
        response = self.client.post(reverse("demo:management_decision_new"), {
            "meeting_date": "2026-10-01",
            "attendees": "مدیرعامل، مدیر عملیات",
            "outcome": ManagementDecision.DISCOVERY,
            "architecture": ManagementDecision.ERPNEXT_CUSTOM,
            "positives": "ردگیری سندها و وضوح جریان",
            "concerns": "مالیات و مهاجرت داده",
            "gap_summary": "قواعد مالی و چند انبار باید در کشف بررسی شوند.",
            "next_step": "اجرای Fit/Gap چهار هفته‌ای",
            "owner": "مدیر عملیات",
            "due_date": "2026-10-15",
            "budget_ceiling": "500000000",
        })
        self.assertRedirects(response, reverse("demo:management_decisions"))
        decision = ManagementDecision.objects.get()
        self.assertTrue(decision.is_actionable)
        self.assertEqual((decision.created_by, decision.updated_by),
                         (self.users[ROLE_MANAGER], self.users[ROLE_MANAGER]))
        audit = AuditEvent.objects.get(action="management_decision_saved",
                                       actor=self.users[ROLE_MANAGER])
        self.assertIsNone(audit.details["previous"])
        self.assertEqual(audit.details["current"]["gap_summary"],
                         "قواعد مالی و چند انبار باید در کشف بررسی شوند.")

    def test_sqlite_backup_validation_and_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, backup, restored = root / "source.sqlite3", root / "backup.sqlite3", root / "restored.sqlite3"
            with closing(sqlite3.connect(source)) as connection:
                for table in ("django_migrations", "auth_user", "demo_item", "demo_order"):
                    connection.execute(f"CREATE TABLE {table} (value TEXT)")
                connection.execute("INSERT INTO demo_item VALUES ('before')")
                connection.commit()
            create_sqlite_backup(source, backup)
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("UPDATE demo_item SET value='after'")
                connection.commit()
            restore_sqlite_backup(backup, restored)
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM demo_item").fetchone()[0], "before")
