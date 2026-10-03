from django.core.management.base import BaseCommand
from django.db import transaction

from demo.accounting import (ensure_chart_of_accounts, post_fulfillment, post_invoice,
                             post_manufacturing, post_opening_stock, post_payment,
                             post_stock_adjustment)
from demo.models import (AuditEvent, Fulfillment, Invoice, JournalEntry, JournalLine, Payment,
                         StockMovement)
from demo.models import WorkOrder
from demo.models import FulfillmentBatch, ProductionBatch, InvoiceCharge
from demo.accounting import post_fulfillment_batch, post_production_batch, post_invoice_charge


class Command(BaseCommand):
    help = "Create missing double-entry journals from existing demo operational documents."

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true",
                            help="Recreate all journals after clearing existing demo journal rows.")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["clear"]:
            AuditEvent.objects.filter(object_type=JournalEntry._meta.model_name).delete()
            JournalLine.objects.all().delete()
            JournalEntry.objects.all().delete()
        ensure_chart_of_accounts()
        before = JournalEntry.objects.count()
        for movement in StockMovement.objects.select_related("item").order_by("created_at", "pk"):
            if movement.source == StockMovement.OPENING:
                post_opening_stock(movement)
            elif movement.source == StockMovement.ADJUSTMENT:
                post_stock_adjustment(movement)
        for fulfillment in Fulfillment.objects.select_related("order").prefetch_related(
                "order__lines__item").order_by("completed_at", "pk"):
            if not fulfillment.order.fulfillment_batches.filter(legacy=False).exists():
                post_fulfillment(fulfillment)
        for batch in FulfillmentBatch.objects.filter(legacy=False).select_related("order"):
            post_fulfillment_batch(batch)
        for invoice in Invoice.objects.select_related("order").order_by("issued_at", "pk"):
            if invoice.charges.filter(batch__legacy=False).exists():
                for charge in invoice.charges.select_related("batch"):
                    post_invoice_charge(charge)
            else:
                post_invoice(invoice)
        for payment in Payment.objects.select_related("invoice__order").order_by("paid_at", "pk"):
            post_payment(payment)
        for work_order in WorkOrder.objects.filter(status=WorkOrder.COMPLETED).select_related(
                "bom__product").prefetch_related("materials__item").order_by("completed_at", "pk"):
            if not work_order.production_batches.filter(legacy=False).exists():
                post_manufacturing(work_order)
        for batch in ProductionBatch.objects.filter(legacy=False).select_related("work_order"):
            post_production_batch(batch)
        created = JournalEntry.objects.count() - before
        self.stdout.write(self.style.SUCCESS(
            f"Accounting journals ready: {JournalEntry.objects.count()} total, {created} created."))
