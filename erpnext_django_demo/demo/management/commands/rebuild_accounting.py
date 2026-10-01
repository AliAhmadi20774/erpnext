from django.core.management.base import BaseCommand
from django.db import transaction

from demo.accounting import (ensure_chart_of_accounts, post_fulfillment, post_invoice,
                             post_opening_stock, post_payment, post_stock_adjustment)
from demo.models import (AuditEvent, Fulfillment, Invoice, JournalEntry, JournalLine, Payment,
                         StockMovement)


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
            post_fulfillment(fulfillment)
        for invoice in Invoice.objects.select_related("order").order_by("issued_at", "pk"):
            post_invoice(invoice)
        for payment in Payment.objects.select_related("invoice__order").order_by("paid_at", "pk"):
            post_payment(payment)
        created = JournalEntry.objects.count() - before
        self.stdout.write(self.style.SUCCESS(
            f"Accounting journals ready: {JournalEntry.objects.count()} total, {created} created."))
