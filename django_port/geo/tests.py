import json
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from .exchange import lookup_exchange_rate
from .models import Country, Currency, CurrencyExchange


class GeoModelTests(TestCase):
    def test_country_requires_real_iso_alpha_two_code(self):
        Country(name="Iran", code="IR").full_clean()
        Country(name="Kosovo", code="xk").full_clean()
        with self.assertRaises(ValidationError):
            Country(name="Unknown", code="ZZ").full_clean()
        with self.assertRaises(ValidationError):
            Country(name="Iran", code="IRN").full_clean()

    def test_currency_fraction_cannot_be_negative(self):
        with self.assertRaises(ValidationError):
            Currency(name="USD", smallest_currency_fraction_value="-0.01").full_clean()

    def test_import_is_idempotent_and_keeps_existing_values(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "countries.json"
            source.write_text(
                json.dumps(
                    {
                        "United States": {
                            "code": "us",
                            "currency": "USD",
                            "currency_symbol": "$",
                            "currency_fraction_units": 100,
                            "timezones": ["America/New_York"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            call_command("import_frappe_geo", source=source, stdout=StringIO())
            self.assertEqual(Country.objects.get(name="United States").time_zones, "America/New_York")
            self.assertTrue(Currency.objects.get(name="USD").enabled)
            Currency.objects.filter(name="USD").update(symbol="custom")
            call_command("import_frappe_geo", source=source, stdout=StringIO())
            self.assertEqual(Country.objects.count(), 1)
            self.assertEqual(Currency.objects.count(), 1)
            self.assertEqual(Currency.objects.get(name="USD").symbol, "custom")


class CurrencyExchangeTests(TestCase):
    def setUp(self):
        self.usd = Currency.objects.create(name="USD", enabled=True)
        self.eur = Currency.objects.create(name="EUR", enabled=True)
        self.day = date(2026, 9, 1)

    def test_rate_identity_purpose_and_exact_date(self):
        buying = CurrencyExchange.objects.create(
            date=self.day, from_currency=self.eur, to_currency=self.usd,
            exchange_rate=Decimal("1.200000000"), for_buying=True, for_selling=False,
        )
        selling = CurrencyExchange.objects.create(
            date=self.day, from_currency=self.eur, to_currency=self.usd,
            exchange_rate=Decimal("1.300000000"), for_buying=False, for_selling=True,
        )
        self.assertIn("Buying", buying.name)
        self.assertIn("Selling", selling.name)
        self.assertEqual(lookup_exchange_rate(self.eur, self.usd, self.day, purpose="buying"), Decimal("1.2"))
        self.assertEqual(lookup_exchange_rate(self.eur, self.usd, self.day, purpose="selling"), Decimal("1.3"))
        self.assertEqual(lookup_exchange_rate(self.usd, self.usd, self.day), Decimal("1"))
        with self.assertRaises(ValidationError):
            lookup_exchange_rate(self.eur, self.usd, self.day)
        with self.assertRaises(ValidationError):
            lookup_exchange_rate(self.eur, self.usd, date(2026, 9, 2))
        buying.exchange_rate = Decimal("1.250000000")
        buying.save()
        self.assertEqual(lookup_exchange_rate(self.eur, self.usd, self.day, purpose="buying"), Decimal("1.25"))

    def test_rate_validation_and_duplicate_purpose(self):
        with self.assertRaises(ValidationError):
            CurrencyExchange.objects.create(date=self.day, from_currency=self.usd, to_currency=self.usd, exchange_rate=1)
        with self.assertRaises(ValidationError):
            CurrencyExchange.objects.create(date=self.day, from_currency=self.eur, to_currency=self.usd, exchange_rate=0)
        with self.assertRaises(ValidationError):
            CurrencyExchange.objects.create(date=self.day, from_currency=self.eur, to_currency=self.usd, exchange_rate=1, for_buying=False, for_selling=False)
        CurrencyExchange.objects.create(date=self.day, from_currency=self.eur, to_currency=self.usd, exchange_rate=1)
        with self.assertRaises(ValidationError):
            CurrencyExchange.objects.create(date=self.day, from_currency=self.eur, to_currency=self.usd, exchange_rate=2)
