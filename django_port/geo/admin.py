from django.contrib import admin

from .models import Country, Currency, CurrencyExchange


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "date_format", "time_format")
    search_fields = ("name", "code")


@admin.register(Currency)
class CurrencyAdmin(admin.ModelAdmin):
    list_display = ("name", "symbol", "enabled", "fraction_units", "number_format")
    list_filter = ("enabled", "number_format")
    search_fields = ("name", "symbol")


@admin.register(CurrencyExchange)
class CurrencyExchangeAdmin(admin.ModelAdmin):
    list_display = ("date", "from_currency", "to_currency", "exchange_rate", "for_buying", "for_selling")
    list_filter = ("date", "from_currency", "to_currency", "for_buying", "for_selling")
    search_fields = ("name",)

    def get_readonly_fields(self, request, obj=None):
        return ("name", "date", "from_currency", "to_currency", "for_buying", "for_selling") if obj else ()
