from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounting.models import CostCenter
from organizations.models import Company


class Command(BaseCommand):
    help = "Create ERPNext-style root and Main cost centers for an existing company."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        company = Company.objects.select_for_update().filter(pk=options["company"]).first()
        if company is None:
            raise CommandError(f"Company does not exist: {options['company']}")
        root = CostCenter.objects.filter(company=company, parent_cost_center__isnull=True).first()
        if root is None:
            root = CostCenter.objects.create(company=company, cost_center_name=company.name, is_group=True)
        main = CostCenter.objects.filter(company=company, parent_cost_center=root, cost_center_name="Main").first()
        if main is None:
            main = CostCenter.objects.create(company=company, cost_center_name="Main", parent_cost_center=root)
        if main.is_group or main.disabled:
            raise CommandError("Existing Main cost center must be an enabled leaf.")
        if not company.cost_center_id:
            company.cost_center = main
            company.save(update_fields=("cost_center",))
        self.stdout.write(self.style.SUCCESS(f"Root: {root.name}; Main: {main.name}"))
