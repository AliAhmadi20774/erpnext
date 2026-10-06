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

    def test_auto_create_standard_chart_on_company_creation(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        company = Company.objects.create(
            name="Auto Standard Co",
            abbr="ASC",
            country=self.country,
            default_currency=usd,
            create_chart_of_accounts_based_on="Standard Template",
        )
        self.assertEqual(company.chart_of_accounts, "Standard")
        accounts = company.accounts.all()
        self.assertGreater(accounts.count(), 50)
        self.assertIsNotNone(company.default_receivable_account)
        self.assertEqual(company.default_receivable_account.account_type, "Receivable")
        self.assertIsNotNone(company.default_payable_account)
        self.assertEqual(company.default_payable_account.account_type, "Payable")
        self.assertIsNotNone(company.default_inventory_account)
        self.assertEqual(company.default_inventory_account.account_type, "Stock")
        self.assertIsNotNone(company.stock_adjustment_account)

    def test_auto_create_numbered_chart_on_company_creation(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        company = Company.objects.create(
            name="Auto Numbered Co",
            abbr="ANC",
            country=self.country,
            default_currency=usd,
            chart_of_accounts="Standard with Numbers",
        )
        self.assertEqual(company.create_chart_of_accounts_based_on, "Standard Template")
        accounts = company.accounts.all()
        self.assertGreater(accounts.count(), 50)
        self.assertTrue(all(a.account_number for a in accounts))

    def test_auto_create_country_template_chart(self):
        in_currency = Currency.objects.create(name="INR", enabled=True)
        india = Country.objects.create(name="India", code="IN")
        company = Company.objects.create(
            name="Auto India Co",
            abbr="AIC",
            country=india,
            default_currency=in_currency,
            create_chart_of_accounts_based_on="Standard Template",
        )
        self.assertEqual(company.chart_of_accounts, "India - Chart of Accounts")
        accounts = company.accounts.all()
        self.assertGreater(accounts.count(), 50)

    def test_auto_create_chart_from_existing_company(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        parent = Company.objects.create(
            name="Source Company",
            abbr="SRC",
            country=self.country,
            default_currency=usd,
            create_chart_of_accounts_based_on="Standard Template",
            is_group=True,
        )
        parent_accounts_count = parent.accounts.count()
        self.assertGreater(parent_accounts_count, 50)

        # Child company with create_chart_of_accounts_based_on="Existing Company"
        child = Company.objects.create(
            name="Destination Company",
            abbr="DST",
            country=self.country,
            default_currency=usd,
            create_chart_of_accounts_based_on="Existing Company",
            existing_company=parent,
        )
        child_accounts = child.accounts.all()
        self.assertEqual(child_accounts.count(), parent_accounts_count)
        # Verify suffix changed
        debtors = child.accounts.get(account_name="Debtors")
        self.assertTrue(debtors.name.endswith("- DST"))
        self.assertEqual(child.default_receivable_account, debtors)

    def test_child_company_inherits_parent_chart_by_default(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        parent = Company.objects.create(
            name="Parent Corp",
            abbr="PC",
            country=self.country,
            default_currency=usd,
            create_chart_of_accounts_based_on="Standard Template",
            is_group=True,
        )
        parent_count = parent.accounts.count()

        child = Company.objects.create(
            name="Sub Branch",
            abbr="SB",
            country=self.country,
            default_currency=usd,
            parent_company=parent,
        )
        self.assertEqual(child.create_chart_of_accounts_based_on, "Existing Company")
        self.assertEqual(child.existing_company, parent)
        self.assertEqual(child.accounts.count(), parent_count)

    def test_chart_creation_validation_errors(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        # Existing company missing
        with self.assertRaises(ValidationError):
            Company(
                name="Bad Company 1",
                abbr="BC1",
                country=self.country,
                default_currency=usd,
                create_chart_of_accounts_based_on="Existing Company",
            ).save()

        # Existing company cannot be self
        with self.assertRaises(ValidationError):
            Company(
                name="Bad Company 2",
                abbr="BC2",
                country=self.country,
                default_currency=usd,
                create_chart_of_accounts_based_on="Existing Company",
                existing_company_id="Bad Company 2",
            ).save()

        # Unknown template
        with self.assertRaises(ValidationError):
            Company(
                name="Bad Company 3",
                abbr="BC3",
                country=self.country,
                default_currency=usd,
                chart_of_accounts="Nonexistent Unknown Template",
            ).save()

