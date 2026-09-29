from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from organizations.models import Company

from .models import FiscalYear, FiscalYearCompany


@transaction.atomic
def create_fiscal_year(*, year, start_date, end_date, companies=(), is_short_year=False):
    """Create a global year or a year assigned to explicit companies."""
    companies = tuple(companies)
    if any(not isinstance(company, Company) for company in companies):
        raise TypeError("companies must contain Company instances")
    if len({company.pk for company in companies}) != len(companies):
        raise ValidationError("A company cannot appear twice in one fiscal year.")
    if FiscalYear.objects.filter(pk=year).exists():
        raise ValidationError("Fiscal year name already exists.")
    fiscal_year = FiscalYear(
        year=year, year_start_date=start_date, year_end_date=end_date,
        is_short_year=is_short_year, all_companies=not companies,
    )
    fiscal_year.save()
    for company in companies:
        FiscalYearCompany.objects.create(fiscal_year=fiscal_year, company=company)
    return fiscal_year


def resolve_fiscal_year(posting_date, company):
    """Prefer a company year, then an applicable global year for the date."""
    applicable = FiscalYear.objects.filter(
        disabled=False,
        year_start_date__lte=posting_date,
        year_end_date__gte=posting_date,
    )
    scoped = applicable.filter(all_companies=False, company_links__company=company).order_by("-year_start_date", "year").first()
    fiscal_year = scoped or applicable.filter(all_companies=True).order_by("-year_start_date", "year").first()
    if fiscal_year is None:
        raise ValidationError("No active fiscal year covers this company and posting date.")
    return fiscal_year


def resolve_consecutive_fiscal_years(*, company, from_fiscal_year, to_fiscal_year):
    """Return active, gap-free fiscal years selected for one company."""
    if not isinstance(company, Company) or not all(isinstance(year, FiscalYear)
                                                   for year in (from_fiscal_year, to_fiscal_year)):
        raise TypeError("Select a company and fiscal years.")
    if from_fiscal_year.year_start_date > to_fiscal_year.year_start_date:
        raise ValidationError("From fiscal year must not follow To fiscal year.")
    years = []
    cursor = from_fiscal_year.year_start_date
    while cursor <= to_fiscal_year.year_end_date:
        year = resolve_fiscal_year(cursor, company)
        if year.year_start_date != cursor or year.year_end_date > to_fiscal_year.year_end_date:
            raise ValidationError("Select consecutive fiscal years for this company.")
        years.append(year)
        cursor = year.year_end_date + timedelta(days=1)
    if years[0].pk != from_fiscal_year.pk or years[-1].pk != to_fiscal_year.pk:
        raise ValidationError("Select fiscal years applicable to this company.")
    return tuple(years)
