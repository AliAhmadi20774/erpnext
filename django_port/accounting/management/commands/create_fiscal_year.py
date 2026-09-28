from datetime import date

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from accounting.fiscal import create_fiscal_year
from organizations.models import Company


class Command(BaseCommand):
    help = "Create a global fiscal year or assign it to one or more companies."

    def add_arguments(self, parser):
        parser.add_argument("--year", required=True)
        parser.add_argument("--start", required=True, help="YYYY-MM-DD")
        parser.add_argument("--end", required=True, help="YYYY-MM-DD")
        parser.add_argument("--company", action="append", default=[], help="Repeat for each company; omit for a global year")
        parser.add_argument("--short-year", action="store_true")

    def handle(self, *args, **options):
        try:
            start = date.fromisoformat(options["start"])
            end = date.fromisoformat(options["end"])
        except ValueError as exc:
            raise CommandError("Start and end must be YYYY-MM-DD dates.") from exc
        names = options["company"]
        companies = list(Company.objects.filter(pk__in=names))
        missing = set(names) - {company.pk for company in companies}
        if missing:
            raise CommandError(f"Unknown companies: {', '.join(sorted(missing))}")
        try:
            fiscal_year = create_fiscal_year(
                year=options["year"], start_date=start, end_date=end,
                companies=companies, is_short_year=options["short_year"],
            )
        except ValidationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Created fiscal year {fiscal_year.year}."))
