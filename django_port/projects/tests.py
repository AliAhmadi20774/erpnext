from datetime import date

from django.core.exceptions import ValidationError
from django.test import TestCase

from geo.models import Country, Currency
from organizations.models import Company

from .models import Project


class ProjectTests(TestCase):
    def test_unique_name_and_date_range(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        Project.objects.create(name="PROJ-1", project_name="Website", company=company,
                               expected_start_date=date(2025, 1, 1), expected_end_date=date(2025, 2, 1))
        with self.assertRaises(ValidationError):
            Project.objects.create(name="PROJ-2", project_name="Website", company=company)
        with self.assertRaises(ValidationError):
            Project.objects.create(name="PROJ-3", project_name="Late", company=company,
                                   expected_start_date=date(2025, 2, 1), expected_end_date=date(2025, 1, 1))
