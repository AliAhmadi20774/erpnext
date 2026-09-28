from decimal import Decimal

from django.core.exceptions import ValidationError

from .models import CurrencyExchange


def lookup_exchange_rate(from_currency, to_currency, on_date, *, purpose=None):
    """Find a manually recorded rate for the exact date, with no remote or stale fallback."""
    from_id = getattr(from_currency, "pk", from_currency)
    to_id = getattr(to_currency, "pk", to_currency)
    if from_id == to_id:
        return Decimal("1")
    if purpose not in (None, "buying", "selling"):
        raise ValueError("Purpose must be buying, selling, or None.")
    rates = CurrencyExchange.objects.filter(
        date=on_date, from_currency_id=from_id, to_currency_id=to_id,
    )
    if purpose == "buying":
        rates = rates.filter(for_buying=True)
    elif purpose == "selling":
        rates = rates.filter(for_selling=True)
    values = set(rates.values_list("exchange_rate", flat=True))
    if not values:
        raise ValidationError(f"No exchange rate is recorded for {from_id} to {to_id} on {on_date}.")
    if len(values) != 1:
        raise ValidationError("Multiple exchange rates apply; specify a purpose or an explicit rate.")
    return values.pop()
