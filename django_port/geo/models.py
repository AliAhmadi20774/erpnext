from decimal import Decimal

import pycountry
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


NUMBER_FORMATS = (
    "#,###.##",
    "#.###,##",
    "# ###.##",
    "# ###,##",
    "#'###.##",
    "#, ###.##",
    "#,##,###.##",
    "#,###.###",
    "#.###",
    "#,###",
)


class Country(models.Model):
    name = models.CharField(max_length=140, primary_key=True)
    code = models.CharField(max_length=2)
    date_format = models.CharField(max_length=140, blank=True)
    time_format = models.CharField(max_length=140, default="HH:mm:ss")
    time_zones = models.TextField(blank=True)

    class Meta:
        db_table = "country"
        verbose_name_plural = "countries"
        ordering = ("name",)

    def clean(self):
        super().clean()
        if self.code:
            # Frappe's bundled country list includes Kosovo as XK.
            if self.code.upper() == "XK":
                return
            try:
                country = pycountry.countries.lookup(self.code)
            except LookupError as exc:
                raise ValidationError({"code": "Enter a valid ISO 3166 alpha-2 code."}) from exc
            if country.alpha_2 != self.code.upper():
                raise ValidationError({"code": "Enter a valid ISO 3166 alpha-2 code."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Currency(models.Model):
    name = models.CharField(max_length=140, primary_key=True)
    enabled = models.BooleanField(default=False)
    fraction = models.CharField(max_length=140, blank=True)
    fraction_units = models.IntegerField(default=0)
    smallest_currency_fraction_value = models.DecimalField(
        max_digits=21,
        decimal_places=9,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
    )
    symbol = models.CharField(max_length=140, blank=True)
    symbol_on_right = models.BooleanField(default=False)
    number_format = models.CharField(
        max_length=20,
        blank=True,
        choices=[(value, value) for value in NUMBER_FORMATS],
    )

    class Meta:
        db_table = "currency"
        verbose_name_plural = "currencies"

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class CurrencyExchange(models.Model):
    name = models.CharField(max_length=140, primary_key=True, blank=True, editable=False)
    date = models.DateField(db_index=True)
    from_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="exchange_rates_from")
    to_currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="exchange_rates_to")
    exchange_rate = models.DecimalField(max_digits=21, decimal_places=9, validators=[MinValueValidator(Decimal("0.000000001"))])
    for_buying = models.BooleanField(default=True)
    for_selling = models.BooleanField(default=True)

    class Meta:
        db_table = "currency_exchange"
        ordering = ("-date", "-name")
        constraints = [
            models.UniqueConstraint(fields=("date", "from_currency", "to_currency", "for_buying", "for_selling"), name="unique_currency_exchange_purpose"),
            models.CheckConstraint(condition=~models.Q(from_currency=models.F("to_currency")), name="currency_exchange_different_currencies"),
            models.CheckConstraint(condition=models.Q(for_buying=True) | models.Q(for_selling=True), name="currency_exchange_has_purpose"),
            models.CheckConstraint(condition=models.Q(exchange_rate__gt=0), name="currency_exchange_positive_rate"),
        ]

    def clean(self):
        super().clean()
        if self.from_currency_id and self.from_currency_id == self.to_currency_id:
            raise ValidationError("Source and destination currencies must differ.")
        if not self.for_buying and not self.for_selling:
            raise ValidationError("Exchange rate must apply to buying or selling.")
        if self.exchange_rate is not None and self.exchange_rate <= 0:
            raise ValidationError({"exchange_rate": "Exchange rate must be positive."})

    def save(self, *args, **kwargs):
        if not self.name and self.date and self.from_currency_id and self.to_currency_id:
            purpose = "Selling-Buying" if self.for_buying and self.for_selling else "Buying" if self.for_buying else "Selling"
            self.name = f"{self.date.isoformat()}-{self.from_currency_id}-{self.to_currency_id}-{purpose}"
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and any(
            getattr(old, field) != getattr(self, field)
            for field in ("date", "from_currency_id", "to_currency_id", "for_buying", "for_selling")
        ):
            raise ValidationError("Create a new rate instead of changing its date, currencies, or purpose.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name
