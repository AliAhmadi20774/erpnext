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
        source.add_argument("--template", default="Standard", help="Chart template name (e.g. Standard, Standard with Numbers, or verified country chart)")
        source.add_argument("--existing-company", help="Copy chart from another existing company")
        source.add_argument("--source", type=Path, help="ERPNext chart JSON containing a tree object")

    def handle(self, *args, **options):
        try:
            company = Company.objects.get(pk=options["company"])
        except Company.DoesNotExist as exc:
            raise CommandError(f"Company does not exist: {options['company']}") from exc
        try:
            existing_company = None
            if options.get("existing_company"):
                try:
                    existing_company = Company.objects.get(pk=options["existing_company"])
                except Company.DoesNotExist as exc:
                    raise CommandError(f"Existing company does not exist: {options['existing_company']}") from exc
            chart = load_chart(
                template=options["template"] if not options.get("existing_company") and not options.get("source") else None,
                source=options["source"],
                existing_company=existing_company,
            )
            plan = plan_chart(chart, company)
            created = install_chart(company, plan)
        except (OSError, ValueError, ValidationError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Created {created} accounts for {company.name}; chart contains {len(plan)} accounts."))
