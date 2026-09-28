from django.core.exceptions import ValidationError
from django.test import TestCase

from geo.models import Country, Currency

from .models import Company


class CompanyModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.country = Country.objects.create(name="Iran", code="IR")
        cls.currency = Currency.objects.create(name="IRR")

    def test_company_abbreviation_and_reporting_currency(self):
        company = Company.objects.create(
            name="Example Trading Company", country=self.country, default_currency=self.currency
        )
        self.assertEqual(company.abbr, "ETC")
        self.assertEqual(company.reporting_currency, self.currency)

    def test_parent_must_be_group(self):
        parent = Company.objects.create(
            name="Parent", country=self.country, default_currency=self.currency
        )
        child = Company(
            name="Child",
            country=self.country,
            default_currency=self.currency,
            parent_company=parent,
        )
        with self.assertRaises(ValidationError):
            child.save()

    def test_group_parent_cannot_be_its_own_child(self):
        parent = Company.objects.create(
            name="Parent", country=self.country, default_currency=self.currency, is_group=True
        )
        child = Company.objects.create(
            name="Child",
            country=self.country,
            default_currency=self.currency,
            parent_company=parent,
            is_group=True,
        )
        parent.parent_company = child
        with self.assertRaises(ValidationError):
            parent.save()

    def test_child_inherits_parent_reporting_currency(self):
        reporting = Currency.objects.create(name="USD")
        parent = Company.objects.create(
            name="Parent",
            country=self.country,
            default_currency=self.currency,
            reporting_currency=reporting,
            is_group=True,
        )
        child = Company.objects.create(
            name="Child",
            country=self.country,
            default_currency=self.currency,
            parent_company=parent,
        )
        self.assertEqual(child.reporting_currency, reporting)
