from django.contrib import admin
from django.urls import path

from accounting.report_views import balance_sheet_comparison_view, balance_sheet_view, balance_sheet_yearly_view, closing_balance_view, general_ledger_view, profit_and_loss_comparison_view, profit_and_loss_view, profit_and_loss_yearly_view, trial_balance_for_party_view, trial_balance_simple_view, trial_balance_view, voucher_wise_balance_view
from stock.report_views import stock_account_comparison_view, stock_ageing_view, stock_analytics_view, stock_balance_view, stock_invariant_view, stock_ledger_view, stock_projected_qty_view, stock_variance_view, total_stock_summary_view, warehouse_balance_view


urlpatterns = [
    path("admin/", admin.site.urls),
    path("reports/general-ledger/", general_ledger_view, name="general_ledger_report"),
    path("reports/stock-ledger/", stock_ledger_view, name="stock_ledger_report"),
    path("reports/stock-balance/", stock_balance_view, name="stock_balance_report"),
    path("reports/stock-ageing/", stock_ageing_view, name="stock_ageing_report"),
    path("reports/stock-analytics/", stock_analytics_view, name="stock_analytics_report"),
    path("reports/stock-projected-qty/", stock_projected_qty_view, name="stock_projected_qty_report"),
    path("reports/stock-ledger-invariant-check/", stock_invariant_view, name="stock_invariant_report"),
    path("reports/stock-ledger-variance/", stock_variance_view, name="stock_variance_report"),
    path("reports/warehouse-wise-stock-balance/", warehouse_balance_view, name="warehouse_balance_report"),
    path("reports/total-stock-summary/", total_stock_summary_view, name="total_stock_summary_report"),
    path("reports/stock-account-comparison/", stock_account_comparison_view, name="stock_account_comparison_report"),
    path("reports/account-closing-balances/", closing_balance_view, name="closing_balance_report"),
    path("reports/trial-balance/", trial_balance_view, name="trial_balance_report"),
    path("reports/trial-balance-simple/", trial_balance_simple_view, name="trial_balance_simple_report"),
    path("reports/trial-balance-for-party/", trial_balance_for_party_view, name="trial_balance_for_party_report"),
    path("reports/voucher-wise-balance/", voucher_wise_balance_view, name="voucher_wise_balance_report"),
    path("reports/balance-sheet/", balance_sheet_view, name="balance_sheet_report"),
    path("reports/balance-sheet/comparison/", balance_sheet_comparison_view, name="balance_sheet_comparison_report"),
    path("reports/balance-sheet/yearly/", balance_sheet_yearly_view, name="balance_sheet_yearly_report"),
    path("reports/profit-and-loss/", profit_and_loss_view, name="profit_and_loss_report"),
    path("reports/profit-and-loss/comparison/", profit_and_loss_comparison_view, name="profit_and_loss_comparison_report"),
    path("reports/profit-and-loss/yearly/", profit_and_loss_yearly_view, name="profit_and_loss_yearly_report"),
]
