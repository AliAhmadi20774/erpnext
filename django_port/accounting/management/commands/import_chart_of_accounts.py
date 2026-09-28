from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounting.chart_import import STANDARD_TEMPLATES, install_chart, load_chart, plan_chart
from organizations.models import Company


class Command(BaseCommand):
    help = "Import an ERPNext chart of accounts into a company with no existing accounts."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Existing Django Company name")
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--template", choices=tuple(STANDARD_TEMPLATES), default="Standard")
        source.add_argument("--source", type=Path, help="ERPNext chart JSON containing a tree object")

    def handle(self, *args, **options):
        try:
            company = Company.objects.get(pk=options["company"])
        except Company.DoesNotExist as exc:
            raise CommandError(f"Company does not exist: {options['company']}") from exc
        try:
            chart = load_chart(template=options["template"], source=options["source"])
            plan = plan_chart(chart, company)
            created = install_chart(company, plan)
        except (OSError, ValueError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Created {created} accounts for {company.name}; chart contains {len(plan)} accounts."))
