import json
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from geo.models import Country, Currency


DEFAULT_SOURCE = Path(settings.BASE_DIR) / "reference_data" / "frappe_country_info.json"
DEFAULT_ENABLED_CURRENCIES = {"INR", "USD", "GBP", "EUR", "AED", "AUD", "JPY", "CNY", "CHF"}


class Command(BaseCommand):
    help = "Import Frappe's country and currency reference data without overwriting edits."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)

    @transaction.atomic
    def handle(self, *args, **options):
        source = options["source"]
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read country data {source}: {exc}") from exc
        if not isinstance(data, dict):
            raise CommandError("Country fixture must be a JSON object")

        countries_created = 0
        currencies_created = 0
        for name, values in data.items():
            if not isinstance(values, dict) or not values.get("code"):
                raise CommandError(f"Country {name} has no ISO code")

            _, created = Country.objects.get_or_create(
                name=name,
                defaults={
                    "code": values["code"],
                    "date_format": values.get("date_format") or "dd-mm-yyyy",
                    "time_format": values.get("time_format") or "HH:mm:ss",
                    "time_zones": "\n".join(values.get("timezones") or []),
                },
            )
            countries_created += created

            currency_name = values.get("currency")
            if currency_name:
                _, created = Currency.objects.get_or_create(
                    name=currency_name,
                    defaults={
                        "enabled": currency_name in DEFAULT_ENABLED_CURRENCIES,
                        "fraction": values.get("currency_fraction") or "",
                        "fraction_units": values.get("currency_fraction_units") or 0,
                        "smallest_currency_fraction_value": Decimal(
                            str(values.get("smallest_currency_fraction_value") or 0)
                        ),
                        "symbol": values.get("currency_symbol") or "",
                        "number_format": values.get("number_format") or "",
                    },
                )
                currencies_created += created

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {countries_created} countries and {currencies_created} currencies"
            )
        )
