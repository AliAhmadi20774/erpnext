# ERPNext to Django porting tracker

Source: the ERPNext 17 development tree in the parent directory. The source
contains roughly 3,000 Python files and 534 ERPNext DocType definitions. It
also relies on DocTypes and behavior supplied by the separate Frappe framework.
This tracker records completed behavior, not merely copied tables.
The per-DocType index is in [source_inventory.csv](source_inventory.csv).
The full artifact [checklist](CHECKLIST.md) is generated from
`port_progress.json` and the source tree. Update it after every porting step.

| Source module | DocTypes |
| --- | ---: |
| Accounts | 191 |
| Stock | 78 |
| Manufacturing | 49 |
| Setup | 40 |
| CRM | 28 |
| Assets | 26 |
| Other modules combined | 122 |

## Completion criteria for each feature

- Fields, relationships, defaults, and validation
- Business operations and their transaction boundaries
- Roles, document permissions, and company scoping
- API, forms, lists, reports, and print behavior where applicable
- Data migration and behavioral tests against ERPNext examples

## Sequence

| Area | Initial dependencies | Status |
| --- | --- | --- |
| UOM category and UOM | None | Models, admin, and fixture import implemented; parity open |
| Company, currency, country | Frappe core reference data | Country and currency fields, reference import, and company foundation implemented; parity open |
| Item group and item | UOM, company, stock rules | Tree and core item models implemented; parity open |
| Price list and item price | Item, UOM, currency, country | Core models, admin, and exact-UOM lookup implemented; parity open |
| Customer and supplier | Company, groups, contacts, accounts | Core party and group models implemented; contact and account workflows open |
| Warehouse | Company, account, stock rules | Model, tree, account resolution, defaults, and ledger guards implemented; parity open |
| Sales and buying | Parties, items, pricing, taxes | Not started |
| Stock Entry | Items, warehouses, stock ledger | Receipt, issue, and transfer submission (including backdated replay), cancellation, receipt rate correction, and perpetual-inventory GL implemented; parity open |
| Stock Reconciliation | Items, warehouses, stock entry | Current count, optional zero-quantity direct value adjustment or paired-entry reset (both backdated), and multi-row backdated quantity count implemented; new stock and GL rows use native voucher identity, with linked Stock Entries retained for snapshots and replay |
| Stock ledger | Items, warehouses, valuation | Bin, immutable ledger, and FIFO/LIFO/moving-average posting foundation implemented; parity open |
| Accounting | Company, chart of accounts, posting rules | Ledger, Journal Entry, closing, and core financial-report foundations implemented; parity open |
| Manufacturing, assets, projects, and other modules | Transaction foundations | Basic Project model implemented; other transaction foundations open |

## UOM source mapping

| ERPNext DocType | Django model | Source |
| --- | --- | --- |
| UOM Category | `catalog.UOMCategory` | `erpnext/stock/doctype/uom_category/` |
| UOM | `catalog.UnitOfMeasure` | `erpnext/setup/doctype/uom/` |

The first slice preserves these DocType fields and names. The ERPNext setup
fixture imported 239 UOMs and 17 categories in a local verification run. The
import does not overwrite existing records. ERPNext's role permissions,
translations, naming behavior, and use in stock transactions are not
implemented yet. They remain open work for UOM parity.

## Company, country, and currency source mapping

Country and currency fields come from Frappe's
[Country](https://github.com/frappe/frappe/blob/develop/frappe/geo/doctype/country/country.json)
and [Currency](https://github.com/frappe/frappe/blob/develop/frappe/geo/doctype/currency/currency.json)
DocTypes. They are represented by `geo.Country` and `geo.Currency`.
The bundled Frappe data contains Kosovo with the `XK` code; country validation
accepts that entry alongside ISO 3166 alpha-2 codes.
`organizations.Company` currently covers company identity, country, currencies,
group hierarchy, core contact fields, perpetual-inventory settings, and default
inventory account and warehouse links from `erpnext/setup/doctype/company/`.
Its remaining account defaults, tax, full chart setup, nested set, and transaction
rules remain open.

## Item group and item source mapping

`catalog.ItemGroup` models the group hierarchy and maintains `lft` and `rgt`
positions on model saves and deletes. It seeds ERPNext's initial `All Item
Groups` and `Default` records. Company-specific defaults, group taxes, and
Frappe permissions remain open.

`catalog.Item` currently models the item code, name, group, stock UOM, basic
sales/purchase/stock flags, description, image, and end-of-life date. Its
per-item UOM conversion table also preserves the base unit with factor 1 and
clears old conversions if the base unit changes. Global UOM conversion lookup
is available as a fallback when no item factor
exists. Basic pricing is now linked through `ItemPrice`. An item's stock UOM and
stock-item flag are protected after ledger activity. Variants, serial/batch behavior, other child tables,
and most of the source DocType's fields remain open. Both are partial ports of
`erpnext/setup/doctype/item_group/` and `erpnext/stock/doctype/item/`.

`catalog.ItemUOMConversion` is a partial port of
`erpnext/stock/doctype/uom_conversion_detail/`. Its Frappe child-row identity
and order remain open. A zero factor is filled from a global conversion rule
when available.

`catalog.UOMConversionFactor` stores global factors from
`erpnext/setup/doctype/uom_conversion_factor/` and follows ERPNext's direct,
inverse, shared-source, and shared-target lookup order. Item-specific nonzero
factors take precedence. The importer loaded all 235 reference rows; it
created three UOMs referenced only by that fixture and corrected its one
malformed value (`0.006993s` to `0.006993`). Frappe naming, permissions,
change history, and API remain open.

## Warehouse foundation

`stock.Warehouse` is a company-scoped forest with ERPNext-style document names,
parent validation, cycle protection, and `lft`/`rgt` positions rebuilt on create,
move, and delete. A parent must be a group in the same company, while independent
root warehouses remain valid. Direct company or name changes are blocked until a
dedicated rename workflow exists. `stock.WarehouseType` stores the optional type.

An explicit warehouse account must be an enabled Stock ledger for the same
company. Effective-account lookup follows ERPNext's order: the warehouse, its
nearest configured ancestor, the company's default inventory account, then the
company's sole enabled Stock ledger. A leaf created while perpetual inventory is
enabled must resolve an account, except during the standard bootstrap command.
Transit defaults must be enabled leaves in the same company with type `Transit`.

`seed_company_warehouses --company NAME` idempotently creates `All Warehouses`,
`Stores`, `Work In Progress`, `Finished Goods`, and `Goods In Transit`, and fills
the company's empty default and in-transit warehouse fields. Warehouse deletion,
leaf-to-group conversion, and direct account changes are protected after Bin or
Stock Ledger activity. Item Default cleanup, remaining transaction eligibility, rename,
Frappe import, roles, tree UI, reports, forms, and API behavior remain open.

## Bin and stock-ledger foundation

`stock.Bin` stores the single current balance for each item and warehouse,
including ERPNext's planned and reserved quantity columns, projected quantity,
valuation rate, and stock value. `stock.StockLedgerEntry` is an immutable audit
row linked to its Bin, fiscal year, source voucher, and optional project. Both
models are read-only in Django Admin and can only be written by the stock service.

`stock.ledger.post_stock_entries` posts all lines in one database transaction,
locks the company and affected balances, prevents duplicate voucher posting, and
updates each Bin with its ledger row. It implements company-level FIFO, LIFO, and
moving-average valuation, including persisted FIFO/LIFO layers and calculated
outgoing rates. It validates the active stock item and UOM, enabled leaf warehouse,
company, fiscal year, project, whole-number UOM rule, and effective inventory
account. Company valuation method, item stock UOM/type, and used warehouse account
are protected after ledger activity.

Negative stock and direct backdated posting through the low-level service are
intentionally rejected. Supported Stock Entry submission can use its restricted
replay workflow instead, so later rows and balances cannot silently diverge.
Cancellation/reversal for other vouchers, serial and batch bundles, Stock Freeze,
inventory dimensions, other source-document integration and GL workflows,
historical import, full permissions, other reports, forms, and APIs remain open.

The first read-only Stock Ledger report is available at
`/reports/stock-ledger/`. It filters posted, non-cancelled ledger rows by
company and date, with optional item, warehouse subtree, project, and voucher
number filters. It displays incoming and outgoing quantity, balance quantity,
incoming and outgoing rates, valuation rate, balance value, value change, and
source voucher. A CSV download has the same rows. With both an item and a
warehouse selected, and no project or voucher filter, the report sums the last
balance in each included warehouse before the date range into an opening row.
The view requires `stock.view_stockledgerentry`. Serial and batch details,
inventory dimensions, report UOM conversion, advanced opening rules, pagination,
printing, and the full Frappe permission model remain open.

The read-only Stock Balance report at `/reports/stock-balance/` groups active
ledger rows by item and leaf warehouse up to the selected end date. It sums
quantity and value changes before the start date into opening balances and
separates period receipts and issues into positive in/out columns. Zero-quantity
revaluations still contribute to value movement. Filters cover company, dates,
item, item-group subtree, warehouse subtree, warehouse type, and optional
zero-balance rows. The page and CSV require `stock.view_stockledgerentry`.
Closing balances are historical ledger balances, not current Bin values.
Stock Closing Entry snapshots, inventory dimensions, serial/batch views,
ageing, alternate UOM, reserved stock, variant attributes, and full Frappe
permissions remain open.

The Stock Ledger Invariant Check at `/reports/stock-ledger-invariant-check/`
checks one company, item, and leaf warehouse. It recomputes cumulative quantity
and value for every non-cancelled ledger row, compares recorded balances and
valuation rate, checks FIFO/LIFO queue quantity, value, and derived rate, and compares the
latest ledger result with Bin quantity, value, and rate. The option to show
incorrect entries starts with the row before the first discrepancy. Page and
CSV access require both `stock.view_stockledgerentry` and `stock.view_bin`.
It reports differences without changing stock. ERPNext's batch/serial specific
checks and reposting workflow remain open.

Stock Ledger Variance at `/reports/stock-ledger-variance/` scans the company's
Bin item/warehouse pairs and applies the single-pair invariant check. It shows
the first ledger difference matching the selected quantity, value, or valuation
category; if no ledger row matches that category, it can show a final Bin
difference. Filters cover item, leaf warehouse, and disabled items or warehouses.
The page and CSV require both `stock.view_stockledgerentry` and `stock.view_bin`.
The report does not repair data. Batch/serial specific checks, a scalable bulk
scan, and the ERPNext reposting workflow remain open.

Warehouse Wise Stock Balance at `/reports/warehouse-wise-stock-balance/`
aggregates non-cancelled Stock Ledger value changes at each leaf warehouse and
rolls the totals up through the company's warehouse tree. An optional as-of
date limits the ledger scan through the end of that date. Selecting one item
also rolls up its quantity in stock UOM; quantities of different items are
never summed. It includes empty warehouses, optionally includes disabled
warehouses, and exports CSV with
parent and depth fields. Page and CSV access require
`stock.view_stockledgerentry` and `stock.view_warehouse`. Frappe's interactive
tree view remains open.

Total Stock Summary at `/reports/total-stock-summary/` sums nonzero current Bin
quantities and values by item and warehouse or by item and company. An optional
as-of date reconstructs quantity and value from active Stock Ledger Entries.
Each row shows its company's currency. The warehouse view requires a company;
the company view can include every company or filter one. Current page and CSV
results require `stock.view_bin`; historical results additionally require
`stock.view_stockledgerentry`. Full Frappe role rules and historical company
currency changes remain open.

Stock Projected Qty at `/reports/stock-projected-qty/` reads current Bin
quantities, including planned, requested, ordered, reserved, and projected
amounts. It filters by company, item, item-group subtree, brand, and warehouse
subtree, and omits disabled or expired items. Page and CSV require `stock.view_bin`.
Item Reorder settings are stored per item and warehouse and editable in Item
admin. The report shows the reorder level and quantity, plus any shortage below
the projected quantity. POS reservations and full source-document
updates of Bin quantities remain open. Selecting Include UOM
adds converted quantity columns in the page and CSV. Item conversion factors
divide stock quantities; as in ERPNext, missing factors default to 1.

Stock and Account Value Comparison at `/reports/stock-account-comparison/`
compares each voucher's active Stock Ledger value change with the net GL debit
less credit in Stock accounts for a perpetual-inventory company. It folds
Stock Valuation Repost and cancellation GL rows back into the original voucher,
and maps legacy two-entry Stock Reconciliations to their backing GL vouchers.
Filters cover an as-of date, optional start date, and one Stock account; a
selected account restricts stock rows to warehouses currently using it. Only
differences appear in the page and CSV. Both `stock.view_stockledgerentry` and
`accounting.view_glentry` are required. Account history after account remapping,
ERPNext's reposting controls, and full Frappe permissions remain open.

## Stock Entry foundation

`stock.StockEntryType`, `StockEntry`, and `StockEntryDetail` model three supported
ERPNext purposes: Material Receipt, Material Issue, and Material Transfer. Run
`seed_stock_entry_types` to create their standard types. Draft entries and rows
can be edited in Django Admin; the Submit action calls
`stock.entries.submit_stock_entry`. Submission creates the matching immutable
stock ledger rows and updates Bin balances in one transaction. For transfers,
source rows post first and each target receipt takes its source's consumed
valuation layers, preserving the exact transferred value. The voucher stores incoming and outgoing value totals,
while row quantities use the item's stock UOM conversion factor.

For perpetual inventory, the same transaction also posts balanced GL rows.
Material Receipt debits the target warehouse's Stock account and credits the
row's Difference Account or the company's Stock Adjustment Account. Material
Issue reverses that pair using the actual outgoing stock value. Material Transfer
moves value between the source and target warehouse accounts; a transfer between
warehouses sharing one account creates no net GL rows. All accounts must be
enabled company-currency ledgers. A Profit and Loss Difference Account requires
an enabled leaf cost center, selected from the row, document, or company. Opening
receipts require a Balance Sheet Difference Account. Closed accounting periods
and submitted period closings block submission and cancellation. GL and stock writes roll back
together on error.

The Admin Cancel action handles only the three supported purposes. It marks
their ledger rows cancelled, replays later active entries in chronological
order, updates Bin and row valuation snapshots, and appends GL reversals and
balanced valuation corrections. Original rows are retained for audit. A later
negative balance, unsupported stock voucher, ledger/Bin mismatch, or closed
period aborts the whole transaction. Existing submitted entries receive an
accounting snapshot in migration; ambiguous historical GL dimensions must be
reconciled before a safe replay. Backdated submission of the same three purposes
stages its ledger rows, revalues every active row in posting order, rebuilds Bin,
and posts balanced GL corrections for affected later entries. Negative future
stock, closed later periods, and unsupported stock voucher types abort it atomically.

`stock.ReceiptRateCorrection` is a small, auditable source-rate amendment for
submitted Material Receipt rows. A draft specifies the row, new rate, and reason;
its Admin Submit action stores the prior rate and replays stock and perpetual GL
in one transaction. It also handles values propagated through later transfers.
Direct editing of a submitted Stock Entry remains forbidden. The general ERPNext
Repost Item Valuation document, asynchronous jobs, and valuation changes from
other source vouchers are not yet ported.

`stock.StockReconciliation` and its item rows provide a counting workflow.
A row records absolute counted quantity and
the prior and difference quantities; positive differences require an incoming
rate, while negative differences use the existing outgoing valuation. The
service creates linked Material Receipt and/or Material Issue vouchers, so
stock and perpetual GL share the established atomic posting and cancellation
path. A submitted reconciliation can only be cancelled through its own
service. Historical replay is rejected if it would make a posted count
inconsistent; its adjustment quantity is not silently changed. A value-only
row explicitly requests a target valuation rate while keeping counted quantity
unchanged. The default method drains and restores all on-hand units at one
posting time. The optional direct method uses a zero-quantity ledger row with
voucher type and number `Stock Reconciliation`, backed by one Stock Entry for replay,
without physical quantity movement. Both reset FIFO/LIFO
layers to the target rate and post the net GL difference. Direct rows use the
internal `is_value_reset` flag, leaving ERPNext's `is_adjustment_entry` meaning
untouched. Previous value, rate,
and value difference are snapshotted and refreshed after an allowed historical
valuation replay. No-op rows are informational. Backdated quantity counts use each row's
balance as of the document time and atomically replay subsequent stock
valuations and GL, including mixed increases and decreases. Backdated
paired value-only resets stage both backing entries before a single replay, preserving
future FIFO/LIFO/moving-average valuations and GL. Direct value adjustments
can also be backdated and replayed. Their stock and GL rows now refer to the
reconciliation, including valuation reposts and cancellation; linked Stock Entry
snapshots remain internal. Documents needing both issue and receipt entries use
one native stock voucher, posted in issue-first order and replayed together when
backdated. New dual-entry documents also post one native GL voucher and reverse
it in one replay. The `native_gl` flag preserves the separate GL references of
documents submitted before this change. Counts that only increase or only
decrease use the reconciliation's own stock and GL voucher identity, including
during backdated replay and cancellation. Serial/batch detail,
reserved-stock rules, CSV import, and full ERPNext reconciliation semantics
remain open.

Unsupported purposes, transit transfers, negative stock, direct low-level
backdated posting, and general Repost Item Valuation remain unavailable.
Serial and batch handling,
additional costs, source document links, manufacturing, permissions, historical
import, reports, and API behavior remain open.

## Price list and item price source mapping

`catalog.PriceList`, `catalog.PriceListCountry`, and `catalog.ItemPrice` are
partial ports of `erpnext/stock/doctype/price_list/`,
`price_list_country/`, and `item_price/`. They have migrations and Django Admin
forms. A price list must support buying or selling. Its currency and flags
propagate to existing item prices when it is saved. An item price validates
the selected unit, enabled list, date range, and duplicate conditions, and
copies its currency and item details from the linked records.

`find_item_price` selects a price in the requested UOM by date, party, and
batch, then checks its packing multiple. It accepts a customer or supplier
record as a lookup argument. The lookup is a foundation for later
sales and buying documents. It does not yet implement stock-UOM fallback,
UOM-dependent rates, exchange conversion, template and variant rules, or the
full ERPNext transaction pricing flow. Item Price now links to Django customer
and supplier records. Migration 0008 preserves older text-only party names in
legacy columns and links names that already have matching records. The
`reconcile_item_price_parties` command links remaining names after party data
is imported. Default buying and selling price lists,
country applicability, permissions, APIs, and Frappe document naming remain
open in the checklist.

## Customer and supplier foundation

`parties.CustomerGroup`, `SupplierGroup`, and `Territory` implement separate
trees with `lft` and `rgt` positions, one root, parent validation, and protected
children. `seed_party_groups` creates ERPNext's initial groups and the
`Rest Of The World` territory without overwriting existing records. The
country-specific territory created by ERPNext's setup wizard is still open.

`parties.Customer` and `Supplier` hold core identity, group, currency, price
list, internal-company, and hold fields. Customer groups must be leaves and
party-specific default price lists must support the corresponding direction.
Names currently default to the party name; ERPNext naming settings and
duplicate-name suffixes remain open. Credit limits, tax rules, permissions,
document forms and APIs, and transaction behavior are not yet ported.

## Account and party account foundation

`accounting.Account` stores a company-specific account tree, its root and
report type, account currency, group/ledger status, and nested-set positions.
It checks parent company, group status, cycles, duplicate account numbers,
and prevents deleting an account with children. Multiple root accounts are
allowed per company. New accounts default to the company currency. Accounts
created as groups may have an account type, as ERPNext's standard chart does;
converting a typed ledger to a group is still rejected.

`import_chart_of_accounts --company NAME` reads ERPNext's Standard chart from
the checked-out source without importing Frappe. `--template "Standard with
Numbers"` selects the numbered standard chart; `--source PATH` accepts an
ERPNext chart JSON file with a `tree` object. It builds accounts in source
order, attaches the company abbreviation to account names, creates the nested
set, and fills missing company receivable/payable defaults from ledger account
types. It requires an empty company chart. Rerunning an unchanged chart is a
no-op; a different or manually modified chart is rejected without overwriting
accounts. Validation and creation are atomic. Missing currencies must be
created first. ERPNext's translated names, all local charts, account-category
and tax-rate fields, transaction checks, and full Chart of Accounts Importer
workflow are still open.

`accounting.PartyAccount` maps a customer, supplier, customer group, or supplier
group to at most one row per company, with optional normal and advance accounts. Linked accounts must
be enabled ledgers in that company. Their currencies must match each other
and, when the party has a default currency, that currency or the company's
default currency. Account edits also recheck existing party mappings.
`organizations.Company` also has four optional default account fields for
customer receivables, supplier payables, advances received, and advances paid.
`accounting.services.resolve_party_account` chooses the requested normal or
advance account from the party row, then its directly assigned group row,
then the company default. An empty field in a row falls through to the next
level. This matches the source's direct-group lookup; parent group ancestors
are not searched. The Django model enforces one group row per company so
lookup remains unambiguous, while the source group controller does not
explicitly check that uniqueness.

These models are editable in Django Admin. Party account rows also appear on
the customer, supplier, customer group, and supplier group forms.

This does not yet cover automatic chart setup on Company creation, full GL
posting, use of the resolver in transaction documents, account renaming,
all Account fields and controls, or permissions. The ERPNext source allows optional account fields
in a Party Account row, so both mappings are optional here too.

## General ledger foundation

`accounting.GLEntry` records the company, ledger account, posting date,
voucher reference, debit or credit amount, account currency, and optional
customer or supplier. `accounting.ledger.post_gl_entries` accepts a whole
voucher's lines, checks at least two entries and exact debit/credit balance,
validates each account and party, and inserts all rows in one transaction.
The same voucher cannot be posted twice through this service. Ledger rows are
read-only in Django Admin and model-level editing/deletion is blocked.
`account_balance` returns the debit-minus-credit balance of one account as
of an optional date. An account's currency or classification cannot change
after ledger postings exist.

This is an internal foundation for later vouchers. Profit-and-loss accounts require a valid
cost center and cannot be used for opening entries. Reporting currency, account dimensions, GL cancellation
and reversals, outstanding amounts, transaction validation, voucher lifecycle,
authorization, and historical ERPNext GL import remain open. The service does
not verify that its voucher reference names an actual posted document yet.

`geo.CurrencyExchange` stores a positive rate between two distinct currencies
for a date and a buying/selling purpose. `geo.exchange.lookup_exchange_rate`
reads only rates recorded for the exact date and rejects ambiguous purposes.
The GL posting service accepts a foreign-account amount separately from the
company-currency amount, obtains a recorded rate or uses an explicit rate,
converts to the company currency at nine decimal places, and stores the rate
and original amount on the GL row. Voucher balance is checked in company
currency. This uses no stale rate, inverse-rate calculation, pegged currency,
remote rate service, or reporting-currency conversion yet. Those behaviors
and ERPNext's currency exchange settings remain open.

## Fiscal year foundation

`accounting.FiscalYear` holds a name, start/end dates, disabled and short-year
flags, and a global or company-specific scope. `FiscalYearCompany` links a
scoped year to one or more companies. Standard years must run from their start
date through the day before the next anniversary; short years may use another
length. Global years cannot overlap other global years, and two scoped years
cannot overlap for the same company. A scoped year may overlap a global year;
the resolver chooses the scoped year for that company first. This explicit
precedence makes that source-supported overlap deterministic in Django.

`create_fiscal_year` and the matching management command create a year and its
company links atomically. `post_gl_entries` resolves an active applicable year
from the posting date and stores it on each GL row. Dates and scope cannot
change after creation, and a company link used by the ledger cannot be removed.
The GL field is nullable in the schema for future historical imports, but the
posting service requires it. Automatic creation of the next year, fiscal-year
closing, permissions, and Frappe data import remain open.

## Accounting period foundation

`accounting.AccountingPeriod` stores a company, inclusive start and end dates,
disabled flag, and an optional Django group whose members may bypass the period
restriction. It rejects future end dates and overlapping periods for the same
company, including disabled periods. `accounting.ClosedDocument` stores the
document types and closed flags. `create_accounting_period` initializes the
18 document types from ERPNext's `period_closing_doctypes` hook as closed;
the Django Admin does the same when a new period has no manually supplied rows.

`validate_accounting_period` checks date, company, document type, closed flag,
disabled flag, and group membership. As in ERPNext, Bank Clearance is exempt.
`post_gl_entries` calls this check before writing ledger rows and accepts an
optional authenticated Django user for the role exemption. Without a user,
closed periods block posting. Direct document save hooks, the special posting
dates used by Asset and Asset Repair, role mapping
from Frappe, data import, and complete document workflows remain open.

## Period closing voucher foundation

`accounting.PeriodClosingVoucher` stores the fiscal year, company, closing
account, period dates, remarks, and draft/submitted state. Draft validation
requires the first period to start on the fiscal-year start date and later
periods to start immediately after the previous submitted closing. The closing
account must be an enabled company-currency liability or equity ledger account.
`submit_period_closing_voucher` requires any previous fiscal year with GL
activity to have a submitted closing voucher, and applies the accounting-period
restriction to the period end date.

Submission aggregates non-opening profit-and-loss GL rows by account, cost
center, finance book, and project for the period, posts balanced reversals and closing-account entries in
one transaction, and marks the voucher submitted. An empty period can submit
without GL rows. Subsequent GL postings on or before any submitted closing
date are blocked until cancellation and re-closing workflows exist.
Foreign-currency profit-and-loss activity and stock-account
activity are rejected until their full closing workflows are ported. Submitted
vouchers cannot be edited or deleted in the model; Django Admin exposes a
submit action for draft vouchers. Cancellation/reversal, stock valuation and
Stock Closing Entry checks, full Account Closing Balance reporting, custom accounting dimensions,
large-ledger background processing, naming, Frappe import,
and full permissions remain open.

## Account closing balance foundation

`accounting.AccountClosingBalance` records cumulative snapshots for each
account, account currency, cost center, finance book, project, and period-closing flag. Submission of
a Period Closing Voucher copies the previous snapshot, adds ordinary GL
activity in the new period, adds this voucher's closing entries under a
separate flag, and includes balance-sheet opening entries on the first close.
The snapshot is created in the same database transaction as the closing GL
rows. It is read-only in Django Admin and through model/queryset edits.
The read-only Account Closing Balances report shows a submitted voucher's
snapshot with account, cost center, finance book, and project filters. It keeps
ordinary and closing rows separate, shows their company-currency totals, and
exports the filtered rows as CSV under the snapshot view permission.

Amounts in reporting currency use the closing date's recorded exchange rate;
missing or ambiguous rates abort the whole closing. If a company has no
separate reporting currency, its default currency is used at rate 1. Other
custom accounting dimensions, the source's older-rate lookup,
historical Frappe import, cancellation, and integration with financial statements remain
open.

## Finance book foundation

`accounting.FinanceBook` is a global named reference, as in ERPNext. A Company
may select a default Finance Book; a Journal Entry may select a book for all
its GL rows. The GL posting service also accepts a book directly for other
future document types. Period closing aggregates profit-and-loss balances by
cost center and finance book, and carries each book's closing snapshots forward
separately. An empty Finance Book selection remains empty on GL rows, matching
the Journal Entry projection in the source. The basic General Ledger and Trial
Balance reports apply the default-book and unassigned-row filtering rules;
other financial reports, rename, import, permissions, and full API behavior remain open.

## Trial Balance report foundation

`accounting.trial_balance_report.trial_balance_report` provides a read-only
company-currency Trial Balance for dates within a selected fiscal year. It
uses the latest submitted Account Closing Balance snapshot before the range,
then adds intervening GL activity to opening balances. When no snapshot
exists, it reads prior GL activity directly, excluding earlier fiscal years'
profit-and-loss entries. Opening rows within the range count toward opening;
ordinary period activity stays separate. Net or gross opening and closing
balances, gross period debit and credit, and parent-account rollups are shown in the web
report and CSV. Supported filters are cost center subtree, project, and Finance
Book. Users may include closing entries independently in opening balances and
current period activity, show unclosed prior-year profit-and-loss balances,
and choose whether to show net or gross balances, group accounts, or zero-value accounts. An optional
presentation currency converts company-currency account amounts using the
latest recorded rate on or before the report end date and displays the rate's
date. Missing or ambiguous latest rates are rejected. Ledger posting and period
closing retain their exact-date rate rules. ERPNext's special handling for
foreign account currencies, custom dimensions, and full report options remain open.

## Trial Balance (Simple) query report

`accounting.trial_balance_simple_report.trial_balance_simple_report` ports the
separate source SQL report with its sole company filter. It groups uncancelled
GL rows by fiscal year, posting date, and account; sums debit and credit;
and reports the maximum Finance Book name for each group, as the source query
does. A web page and CSV expose the result to users with `view_glentry`.
Frappe role mapping and report presentation remain open.

## Trial Balance for Party foundation

`accounting.trial_balance_for_party_report.trial_balance_for_party_report`
groups uncancelled GL rows by Customer or Supplier. It computes net opening
balances from earlier and explicitly opening entries, gross period debit and
credit, and net closing balances. It supports party and account-subtree filters,
zero-value and zero-closing options, a read-only page, and CSV under
`view_glentry`. Employee, Member, and Shareholder party types, Frappe company
restrictions, party naming settings, and full role behavior remain open.

## Voucher-wise Balance diagnostic report

`accounting.voucher_wise_balance_report.voucher_wise_balance_report` lists
uncancelled GL vouchers with unequal company-currency debit and credit totals.
It supports company, voucher type, and posting-date filters, plus a read-only
page and CSV under `view_glentry`. Company is required to avoid mixing
currencies. Rows are grouped by voucher type and number to avoid merging
different document types that happen to share a number; the ERPNext source
groups by number alone. Normal Django posting rejects unbalanced vouchers,
so this report is most useful for imported historical data.

## Balance Sheet foundation

`accounting.balance_sheet_report.balance_sheet_report` derives a single-date
Balance Sheet from the net Trial Balance and its opening snapshot. Asset rows
use debit-minus-credit balances; liability and equity rows use credit-minus-debit.
Root-account totals avoid double counting group rows. The opening asset versus
liability-and-equity difference is shown as unclosed prior-year profit or loss;
the remaining closing difference is provisional current profit or loss. A
read-only page and CSV expose the rows and balancing totals under `view_glentry`.
Cost-center subtree, Project, Finance Book, and the default-book inclusion option
reuse the Trial Balance filters across both opening and current entries. Optional
presentation currency uses the existing as-of report rate. A separate
comparison page shows accumulated balances at monthly, quarterly,
half-yearly, or yearly period ends within one fiscal year. The final period can
end on the selected To Date. Its account rows are aligned across dates, with
per-period balancing totals in the page and CSV. This version recomputes each
snapshot using the single-date report. Growth view keeps the first period's
amount and replaces later amounts with the percentage change from the preceding
period, using the source report's zero and negative-base rules. The page and
CSV label those percentage columns. Period movement mode subtracts the balance
at the start of each period from its ending balance. It retains the source
report's unclosed prior-year profit/loss line in each period and adjusts the
provisional line to keep the columns balanced. A mid-period From Date uses the
previous day's balance as its baseline. Financial report templates, dimension
grouping, charts, print output, and full
role behavior remain open.

A separate multi-year comparison selects consecutive active fiscal years
applicable to the chosen company. It supports monthly, quarterly, half-yearly,
or yearly columns with accumulated balances or period movement. Each year
starts a new set of periods. Account rows align across years, including accounts
that appear only in later years. Report and Growth views and CSV are available.
Closing a prior year updates its year-end equity and the following year's
opening balances. Missing years and years scoped to another company are
rejected. Report templates, dimension grouping, charts, print output, and
complete Frappe roles remain open.

## Profit and Loss Statement foundation

`accounting.profit_and_loss_report.profit_and_loss_report` uses the selected
fiscal-year date range's Trial Balance activity. Income is credit minus debit;
expense is debit minus credit; net profit or loss is their difference. Root
account totals avoid counting group rows twice. Period Closing Voucher entries
are excluded so historical income and expenses remain visible after closing.
The report accepts cost-center subtree, Project, Finance Book, default-book
inclusion, presentation currency, and zero-row options. A read-only page and
CSV require `view_glentry`.

The comparison page shows monthly, quarterly, half-yearly, or yearly columns
inside one fiscal year. Period activity is the default; accumulated values can
also be selected. The Total column sums period activity or takes the final
accumulated value. A partial first or last period uses the selected date range.
Closing entries remain excluded in every column. Account rows align across
periods, including accounts with activity in only one period.

Growth view keeps the first period as an amount and displays later periods as
the percentage change from the preceding period, following the source report's
zero and negative-base rules. Margin view divides each period's account and
summary value by that period's Total Income. A zero income and zero row produce
0%; zero income with a nonzero row yields an undefined value shown as a dash
on the page and an empty CSV cell. The Total column remains an amount in both
views, as in the source report.
The comparison page charts Total Income, Total Expense, and Net Profit/Loss
from the underlying currency amounts, even when Growth or Margin transforms
the table columns. Period activity uses bars; accumulated values use lines.
The SVG chart handles zero and negative results without a chart dependency.

A multi-year comparison shows monthly, quarterly, half-yearly, or yearly Profit
and Loss columns for consecutive active fiscal years applicable to the selected
company. Account rows are aligned across years, including accounts first used
in a later year. Period activity sums into Total; accumulated values restart
at each fiscal year and Total uses the final column. Report, Growth, and Margin
views and CSV use the same underlying values. The fiscal-year sequence check is
shared with the Balance Sheet yearly comparison. Templates, dimension grouping,
print output, advanced currency rules, and full Frappe roles remain open.

## General Ledger report foundation

`accounting.general_ledger_report.general_ledger_report` reads immutable GL
rows for a company and date range. It supports an account subtree, cost-center
subtree, Finance Book, Project, Customer or Supplier, exact voucher and reference voucher
numbers, and ERPNext's
General Ledger rule that unassigned rows
accompany the selected book. With "Include Default Book Entries" selected, an
unspecified book uses the company's default; selecting a different book raises
an error. It returns opening, period, and closing debit/credit totals and a
running balance per account. Opening entries can be shown as rows or included
only in the opening total. The read-only page at `/reports/general-ledger/`
requires Django's `accounting.view_glentry` permission. The page exports the
same filtered rows and totals as CSV under the same permission.

"Disable opening balance calculation" ignores ordinary entries before the
start date while retaining explicit opening entries before that date. Opening
entries within the selected period appear as rows when this option is set.
This follows the source report's distinction between historical turnover and
entries explicitly marked as opening.

"Show remarks" adds the stored GL remarks to page rows and CSV. HTML escaping
and CSV text escaping protect remarks from being interpreted as markup or
spreadsheet formulas. ERPNext's configurable remark truncation length has not
yet been ported.

With one leaf account selected, "Show account currency" adds opening, period,
closing, and running amounts in that account's currency to the page and CSV.
The company-currency amounts remain visible separately. The option requires a
leaf account so totals never mix currencies from different accounts.

"Group by account" orders rows by account and shows opening, period, and
closing totals for each account with period entries. It accepts all accounts
or a subtree rooted at a group account. Overall totals remain in the report
and CSV. As in the source, a leaf-account filter cannot be combined with
account grouping.

"Group by party" creates separate Customer, Supplier, and unassigned groups.
It shows opening, period, and closing totals plus a running party balance in
company currency. Party type and ID form the group key, so identical Customer
and Supplier IDs remain separate. This option cannot be combined with account
grouping or account-currency totals.

"Group by voucher" shows rows and a period total for each voucher type/number
pair. The running balance in that mode is within the voucher. A voucher-number
filter is rejected in this mode, following the source report. Consolidation
of duplicate voucher rows is available separately through "Consolidate voucher
rows". That option sums matching rows of a voucher while keeping party, cost
center, project, Finance Book, and reference voucher distinct. Its row amounts
and running balances are recalculated after consolidation; the same result is
shown in CSV. Full ERPNext consolidation across every accounting dimension and
immutable-ledger creation timestamp remains open.

Journal Entry Account rows carry an optional reference type/name pair into GL
rows. The reference voucher filter matches the GL reference number exactly.
Reference document existence and settlement behavior await the corresponding
document ports.

The source report's transaction currency columns, other opening variants,
snapshot reporting, print layout, pagination, and full Frappe role behavior remain open.

## Project foundation

`projects.Project` stores an explicit ID, unique project name, company, optional
customer and planned dates, status, and active flag. It is editable in Django
Admin. Journal Entry Account and GL Entry have protected Project links. Journal
submission carries the selected Project to the matching GL row, and the GL
report filters opening and period rows by Project. Both posting and reporting
reject a Project from a different company. Task planning, costing, billing,
automatic naming, import, and full Project workflows remain open.

## Journal entry foundation

`accounting.JournalEntry` and `JournalEntryAccount` provide editable drafts for
ordinary Journal Entry and Opening Entry documents. Rows store account-currency
debits or credits, an optional explicit exchange rate, customer or supplier,
cost center, project, and remarks. The document's optional Finance Book applies to every
row. Submission validates at least two rows, one positive
side per row, company/account consistency, the Multi Currency flag, and the
shared ledger's fiscal-year, period, party, currency, cost-center, and balance
rules. The GL voucher type remains `Journal Entry`; Opening Entry sets its GL
opening flag. Header totals and submitted status change only after all GL rows
post successfully in the same transaction. Django Admin exposes draft rows and
a submit action. Submitted headers and rows reject ordinary model edits and
deletes, including when an older in-memory object is used.

Other ERPNext Journal Entry voucher types, naming series, references and
outstanding allocation, advances, cancellation and reversal, cheque and tax
details, inter-company journals, background submission, Frappe import, and
complete permissions remain open.

## Cost center foundation

`accounting.CostCenter` models each company's separate cost-center tree with
one group root named after that company, parent and cycle checks, number
uniqueness, and `lft`/`rgt` positions. The `seed_company_cost_centers --company
NAME` command creates the root and a leaf named `Main` when absent, then sets
the company's default cost center if empty. The command can be rerun without
duplicates. A GL entry on a profit-and-loss account requires an enabled leaf
cost center from the same company. A cost center already used by the ledger
cannot become disabled or a group.

Cost center allocation, accounting dimensions, import, renaming, role
permissions, and ERPNext's full defaulting across vouchers remain open.

## Address and contact foundation

Frappe defines [Address](https://github.com/frappe/frappe/blob/develop/frappe/contacts/doctype/address/address.json),
[Contact](https://github.com/frappe/frappe/blob/develop/frappe/contacts/doctype/contact/contact.json),
[Contact Email](https://github.com/frappe/frappe/blob/develop/frappe/contacts/doctype/contact_email/contact_email.json),
[Contact Phone](https://github.com/frappe/frappe/blob/develop/frappe/contacts/doctype/contact_phone/contact_phone.json),
and [Dynamic Link](https://github.com/frappe/frappe/blob/develop/frappe/core/doctype/dynamic_link/dynamic_link.json)
outside ERPNext's own DocTypes. Their partial Django models live in `contacts`.

Addresses and contacts can each link to multiple customers and suppliers.
`AddressPartyLink` and `ContactPartyLink` use real foreign keys for those two
party types and allow an address or contact to be shared. Customer and supplier
records can select a primary address and contact only when a corresponding
link exists. Contact email and phone rows preserve separate primary flags.
The Frappe dynamic link also supports other DocTypes. `contacts.services`
can create a linked primary contact from email, mobile, and name input, and
a linked primary billing/shipping address from street, city, and country.
Customer and Supplier Django Admin forms expose those quick-entry inputs.
Creation is atomic and idempotent when the party already has a primary record.
Customer and Supplier expose read-only email, mobile, and address display
properties from their currently selected primary records. Customer also
exposes the contact's first and last name. Edits to the linked records are
visible without copying values into party columns. A lone contact email is
automatically primary, including when one email remains after a deletion.
The simple address display does not yet use Frappe's country-specific address
templates, and these derived values are not ORM columns.
ERPNext's full document-event behavior, synchronization of stored source fields, naming,
import, permissions, and full form/API behavior remain open.
