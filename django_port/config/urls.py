from django.contrib import admin
from django.urls import path

from accounting.report_views import closing_balance_view, general_ledger_view, trial_balance_for_party_view, trial_balance_simple_view, trial_balance_view, voucher_wise_balance_view


urlpatterns = [
    path("admin/", admin.site.urls),
    path("reports/general-ledger/", general_ledger_view, name="general_ledger_report"),
    path("reports/account-closing-balances/", closing_balance_view, name="closing_balance_report"),
    path("reports/trial-balance/", trial_balance_view, name="trial_balance_report"),
    path("reports/trial-balance-simple/", trial_balance_simple_view, name="trial_balance_simple_report"),
    path("reports/trial-balance-for-party/", trial_balance_for_party_view, name="trial_balance_for_party_report"),
    path("reports/voucher-wise-balance/", voucher_wise_balance_view, name="voucher_wise_balance_report"),
]
