from datetime import timedelta
from decimal import Decimal
from io import StringIO
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from .tests import AuthenticatedTestCase
from .models import (BillOfMaterials, BOMComponent, Customer, Item, JournalEntry,
                     JournalLine, Order, OrderLine, Supplier)
from .manufacturing import create_work_order, release_work_order, complete_work_order
from .partial_operations import fulfill_partial, produce_partial
from .services import (cancel_order, confirm_order, fulfill_order, issue_invoice,
                       record_opening_stock, record_payment)
from .planning import calculate_requirements


class PartialOperationsTests(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.material = Item.objects.create(sku="PART-M", name="ماده", purchase_price=1000, sale_price=0)
        self.product = Item.objects.create(sku="PART-P", name="محصول", purchase_price=1000,
                                           sale_price=2000)
        record_opening_stock(self.material.pk, 100)
        self.bom = BillOfMaterials.objects.create(code="PART-B", product=self.product, status="active")
        BOMComponent.objects.create(bom=self.bom, item=self.material, quantity=1)
        self.customer = Customer.objects.create(code="PART-C", name="مشتری")
        due = timezone.localdate() + timedelta(days=7)
        self.work = create_work_order(bom_id=self.bom.pk, quantity=40,
            planned_start=timezone.localdate(), due_date=due)
        release_work_order(self.work.pk)
        self.order = Order.objects.create(kind="sales", customer=self.customer, due_date=due)
        self.line = OrderLine.objects.create(order=self.order, item=self.product, quantity=40,
                                             unit_price=2000)
        confirm_order(self.order.pk)

    def assert_balanced(self):
        for entry in JournalEntry.objects.all():
            totals = entry.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
            self.assertEqual(totals["debit"], totals["credit"])

    def test_quality_materials_remaining_and_replay(self):
        key = uuid4()
        batch = produce_partial(self.work.pk, 23, 2, "رد نقص مونتاژ", self.manager, key)
        self.material.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual((self.material.stock, self.product.stock), (75, 23))
        self.assertEqual((self.work.accepted_quantity, self.work.rejected_quantity,
                          self.work.remaining_quantity), (23, 2, 15))
        self.assertEqual(self.work.materials.get().remaining_quantity, 15)
        expense = JournalLine.objects.get(entry__source_type="production_batch", account__code="5300")
        self.assertEqual(expense.debit, 2000)
        self.assertEqual(produce_partial(self.work.pk, 23, 2, "رد نقص مونتاژ", self.manager, key), batch)
        with self.assertRaises(ValidationError):
            produce_partial(self.work.pk, 24, 2, "رد نقص مونتاژ", self.manager, key)
        with self.assertRaises(ValidationError):
            produce_partial(self.work.pk, 16, 0, "قبول")
        with self.assertRaises(ValidationError):
            produce_partial(self.work.pk, 1, 0, "")
        complete_work_order(self.work.pk)
        self.material.refresh_from_db(); self.product.refresh_from_db()
        self.assertEqual((self.material.stock, self.product.stock), (60, 38))
        self.assertEqual(self.work.remaining_quantity, 0)
        self.assert_balanced()

    def test_partial_invoices_payments_and_rebuild(self):
        produce_partial(self.work.pk, 40, 0, "قبول")
        key = uuid4()
        batch = fulfill_partial(self.order.pk, {self.line.pk: 10}, self.manager, key)
        self.assertEqual(fulfill_partial(self.order.pk, {self.line.pk: 10}, self.manager, key), batch)
        with self.assertRaises(ValidationError):
            fulfill_partial(self.order.pk, {self.line.pk: 11}, self.manager, key)
        self.assertEqual((self.line.fulfilled_quantity, self.line.remaining_quantity), (10, 30))
        invoice = issue_invoice(self.order.pk)
        self.assertEqual(invoice.amount, 20000)
        record_payment(invoice.pk, 20000)
        with self.assertRaises(ValidationError):
            issue_invoice(self.order.pk)
        with self.assertRaises(ValidationError):
            cancel_order(self.order.pk)
        fulfill_partial(self.order.pk, {self.line.pk: 5})
        invoice = issue_invoice(self.order.pk)
        self.assertEqual((invoice.amount, invoice.paid, invoice.balance), (30000, 20000, 10000))
        with self.assertRaises(ValidationError):
            record_payment(invoice.pk, 10001)
        fulfill_order(self.order.pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.workflow_label, "در انتظار صورتحساب")
        invoice = issue_invoice(self.order.pk)
        self.assertEqual(invoice.amount, 80000)
        before = JournalEntry.objects.count()
        call_command("rebuild_accounting", "--clear", stdout=StringIO())
        self.assertEqual(JournalEntry.objects.count(), before)
        self.assertFalse(JournalEntry.objects.filter(source_type="fulfillment").exists())
        self.assertFalse(JournalEntry.objects.filter(source_type="invoice").exists())
        self.assert_balanced()
        self.assertContains(self.client.get(reverse("demo:invoice_print", args=[invoice.pk])), "تجمعی")

    def test_stock_shortage_rolls_back_everything_and_rounding_is_cumulative(self):
        self.material.stock = 1
        self.material.save()
        before = JournalEntry.objects.count()
        with self.assertRaises(ValidationError):
            produce_partial(self.work.pk, 2, 0, "قبول")
        self.assertFalse(self.work.production_batches.exists())
        self.assertEqual(JournalEntry.objects.count(), before)
        with self.assertRaises(ValidationError):
            fulfill_partial(self.order.pk, {self.line.pk: 1})
        self.assertFalse(self.order.fulfillment_batches.exists())
        self.material.stock = 100; self.material.save()
        mat = self.work.materials.get(); mat.required_quantity = 41; mat.save()
        produce_partial(self.work.pk, 1, 0, "قبول")
        self.assertEqual(mat.remaining_quantity, 39)
        produce_partial(self.work.pk, 39, 0, "قبول")
        self.material.refresh_from_db()
        self.assertEqual(self.material.stock, 59)

    def test_remaining_purchase_and_production_receipts_and_access(self):
        supplier = Supplier.objects.create(code="PART-S", name="تامین‌کننده")
        purchase = Order.objects.create(kind="purchase", supplier=supplier, due_date=timezone.localdate())
        line = OrderLine.objects.create(order=purchase, item=self.material, quantity=10, unit_price=1000)
        confirm_order(purchase.pk)
        fulfill_partial(purchase.pk, {line.pk: 4})
        self.assertEqual(line.remaining_quantity, 6)
        self.material.stock = 0; self.material.save()
        rows, _ = calculate_requirements(self.product.pk, 20, self.order.due_date,
                                         exclude_plan_id=None)
        # Remove the product receipt to inspect material receipt on its own.
        self.work.status = "cancelled"; self.work.save()
        rows, _ = calculate_requirements(self.product.pk, 20, self.order.due_date)
        self.assertEqual(next(x for x in rows if x["item"].pk == self.material.pk)["scheduled_receipts"], 6)
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_user("partial-sales", password="x"))
        self.assertEqual(self.client.get(reverse("demo:order_partial", args=[self.order.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("demo:work_order_batch", args=[self.work.pk])).status_code, 403)

    def test_seed_example_and_partial_role_pages(self):
        # Separate sample data uses independent products and real stock movements.
        call_command("seed_demo", stdout=StringIO())
        # seed adds samples only when the reference customer exists; reset is deterministic.
        call_command("reset_demo", "--yes", "--no-backup", stdout=StringIO())
        sample = Order.objects.get(notes="DEMO-PARTIAL-SALES")
        work = sample.lines.get().production_plans.get().work_orders.get()
        self.assertEqual((work.accepted_quantity, work.rejected_quantity, work.remaining_quantity), (23, 2, 15))
        self.assertEqual((sample.lines.get().fulfilled_quantity, sample.invoice.balance), (10, 10000))
        self.assertContains(self.client.get(reverse("demo:order_detail", args=[sample.pk])), "تحویل جزئی")
        self.assertContains(self.client.get(reverse("demo:work_order_detail", args=[work.pk])), "مردود")
        self.assertEqual(self.client.get(reverse("demo:order_partial", args=[sample.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("demo:work_order_batch", args=[work.pk])).status_code, 200)
