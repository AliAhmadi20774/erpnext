# Django port of ERPNext

This is a separate Django implementation of ERPNext features. The original
ERPNext source in the parent directory is the behavior and schema reference.
Porting is tracked in [PORTING.md](PORTING.md). A model appearing here does not
mean its whole ERPNext workflow is implemented.

## Local setup

Requires Python 3.10 or newer.

```powershell
cd django_port
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\python manage.py runserver
```

The admin is at `http://127.0.0.1:8000/admin/`. SQLite is only the local
development database. Set `DJANGO_SECRET_KEY` and `DJANGO_DEBUG=0` before
running outside local development.

To import the UOM reference data shipped with ERPNext, run:

```powershell
.venv\Scripts\python manage.py import_erpnext_uoms
```

The import adds missing units and categories. It leaves existing units alone.

To import the Frappe country and currency reference data, run:

```powershell
.venv\Scripts\python manage.py import_frappe_geo
```

The bundled reference file comes from Frappe's
[`country_info.json`](https://github.com/frappe/frappe/blob/develop/frappe/geo/country_info.json)
(retrieved 2026-09-28; SHA-256
`BA7C2CE5961A42B0304B4C7A062EBC453A6CFF6099F1EB86D791C596AD87FD4D`).
The command adds missing records and leaves existing ones unchanged.

To create ERPNext's initial item-group root and `Default` group:

```powershell
.venv\Scripts\python manage.py seed_item_groups
```

After creating an item in the admin, edit it to add UOM conversion rows. The
factor means **stock units per one selected unit**: if the stock UOM is `Nos`,
`Box` with a factor of `12` means one box contains 12 units. The stock UOM row
is created automatically with a factor of 1.

To import ERPNext's global UOM conversion factors after the UOM import:

```powershell
.venv\Scripts\python manage.py import_erpnext_uom_conversions
```

This imports 235 source factors without overwriting existing pairs. Three UOMs
referenced by the source factors but absent from its UOM list are created. The
source's `Meter` to `Ells (UK)` value `0.006993s` is explicitly read as
`0.006993`.

To refresh the source DocType inventory after ERPNext changes:

```powershell
python scripts\build_inventory.py
python scripts\build_checklist.py
```

The full [checklist](CHECKLIST.md) covers DocTypes, reports, print formats,
pages, workspaces, dashboard charts, number cards, and web forms. After each
porting step, update `port_progress.json` and regenerate both files. Mark an
artifact `complete` only after all criteria in [PORTING.md](PORTING.md) pass.

Price Lists and Item Prices can now be entered in Django Admin. Create a
Price List with a currency and at least one of Buying or Selling, then add
an Item Price for an existing item. The item must list the selected UOM in
its conversion rows. The price lookup currently supports the exact UOM and
basic date, party, batch, and packing conditions; transaction integration
remains in progress.

To create ERPNext's initial customer and supplier groups and basic territories:

```powershell
.venv\Scripts\python manage.py seed_party_groups
```

Customer and Supplier records can be entered in Django Admin after creating
their groups. Their transaction and naming workflows remain open in the
[tracker](PORTING.md).

Create a Company and Currency first, then create Account tree roots and ledger
accounts in Django Admin. Customer, Supplier, Customer Group, and Supplier
Group edit pages expose account rows for a selected company. Set company
default receivable, payable, and advance accounts on the Company page if
needed. `resolve_party_account` uses party, direct group, then company order
for normal and advance accounts. Automatic chart setup and accounting
transactions remain open.

To populate an existing company's empty account chart from ERPNext's standard
template:

```powershell
.venv\Scripts\python manage.py import_chart_of_accounts --company "Company Name"
```

Use `--template "Standard with Numbers"` for account numbers, or `--source
PATH_TO_ERPNext_CHART.json` for a chart JSON file. A repeat import of the same
unchanged chart creates no accounts; an existing different chart is left alone.
The command assigns default receivable and payable accounts when those company
fields are empty. Create any currencies named by a custom chart before import.

The general-ledger foundation stores balanced voucher entries through
`accounting.ledger.post_gl_entries` and exposes read-only GL rows in Django
Admin. It supports company and foreign account currencies. Profit-and-loss entries
require a leaf cost center. To create ERPNext-style root and Main cost centers
for an existing company:

```powershell
.venv\Scripts\python manage.py seed_company_cost_centers --company "Company Name"
```

Foreign-account GL lines can now use a manually entered Currency Exchange for
the posting date, or an explicit rate. Enter the amount in account currency;
the posting service stores both that amount and the converted company amount.
Reporting-currency conversion, allocation, and document posting/cancellation
workflows still need to be ported before this can serve as the full ERPNext
ledger.

Create a fiscal year before posting any GL entries. Omit `--company` for a
global year; repeat it for a year shared by several companies:

```powershell
.venv\Scripts\python manage.py create_fiscal_year --year "2026" --start 2026-01-01 --end 2026-12-31
.venv\Scripts\python manage.py create_fiscal_year --year "2026-27" --start 2026-04-01 --end 2027-03-31 --company "Company Name"
```

Use `--short-year` when the period is intentionally shorter or longer than a
standard year. The GL posting service selects a company-specific year before
an overlapping global year.

Accounting Periods can be created in Django Admin. A new period with no custom
document rows starts with ERPNext's default closed document types. Closed
periods prevent matching GL voucher postings within their date range. Assign
an exempted Django group if particular users may post during the period; code
calling `post_gl_entries` must pass that authenticated user to apply the
exemption. Period closing for transaction forms remains in progress.

Create a Period Closing Voucher draft in Django Admin, choosing a liability or
equity closing account in the company currency. Use **Submit selected period
closing vouchers** from its list page to post the period's profit-and-loss
balances. The first period starts on the fiscal-year start date; later periods
start the day after the previous submitted closing. This currently supports
company-currency GL activity with cost centers, Finance Books, and projects. Foreign-currency profit-and-loss
and stock-account activity are refused until their closing workflows exist.
After submission, GL postings dated on or before the closing date are blocked
until cancellation and re-closing are implemented.
Each submission also creates read-only Account Closing Balance snapshots,
carrying earlier balances forward by cost center, Finance Book, and project. If the company has a separate reporting
currency, record an exchange rate for the closing date first; a missing rate
stops the submission without leaving GL entries behind.

To post a basic Journal Entry, create a draft in Django Admin and add at least
two account rows. Enter one debit or credit amount in each account's currency;
select a cost center for profit-and-loss accounts and a customer or supplier
for receivable or payable accounts. Enable Multi Currency when any account uses
a foreign currency. Use **Submit selected journal entries** from the list
page. Opening Entry is supported for balance-sheet accounts. Submitted entries
are locked; cancellation and other journal variants are still in progress.

Create Finance Books in Django Admin when you need separate accounting views.
Select one on a Journal Entry to carry it onto its GL rows and closing
snapshots. A company may also name its default Finance Book. Financial-report
filtering by book is available in the basic General Ledger report; the other
financial reports remain to be ported.

The read-only General Ledger report is at
`http://127.0.0.1:8000/reports/general-ledger/`. Log in with a user that has
the `view_glentry` permission. Choose a company and date range, then optionally
an account, cost center, Finance Book, Customer or Supplier ID, or exact voucher
number, reference voucher number, or Project. Opening, period, and closing totals are shown in company currency;
the filtered result can be downloaded as CSV. For a selected leaf account, check
"Show account currency" to see separate opening, period, closing, and running
amounts in that account's currency on the page and in CSV. "Group by account"
shows each account's opening, period, and closing totals when all accounts or
a group account is selected. "Group by party" gives separate totals and running
balances for each Customer, Supplier, and unassigned group in company currency.
"Group by voucher" shows each voucher's rows and period totals separately.
The three grouping options and "Consolidate voucher rows" are mutually
exclusive. Consolidation sums matching rows of a
voucher are summed while party, cost center, project, finance book, and reference
stay distinct. Transaction-currency columns remain open.
"Disable opening balance calculation" excludes ordinary entries before the
selected date range while retaining explicitly marked opening entries.
"Show remarks" adds the GL row remarks to the page and CSV. CSV treats remarks
that begin like spreadsheet formulas as text.

The read-only Account Closing Balances report is at
`http://127.0.0.1:8000/reports/account-closing-balances/`. Users need the
`view_accountclosingbalance` permission. Select a company and submitted Period
Closing Voucher, then optionally filter by account, cost center, Finance Book,
or project. Ordinary and closing entries remain separate; the table shows
cumulative debit, credit, and net balance in company currency. The same rows
can be downloaded as CSV.

The basic Trial Balance report is at `http://127.0.0.1:8000/reports/trial-balance/`.
It requires `view_glentry`. Select a company, fiscal year, and date range;
cost center, project, and Finance Book filters are optional. It shows opening
and closing balances and period activity for each account, with a choice of
net or gross opening and closing columns, group accounts, and zero rows. If a submitted
period closing voucher precedes the range, its snapshot supplies the opening
balances, followed by any intervening GL activity. The report is available as CSV.
Closing entries can be included independently in opening balances and current
period activity. The optional unclosed prior-year P&L setting shows those
balances when an earlier fiscal year was not fully closed.
An optional presentation currency converts company-currency amounts with the
latest manually recorded exchange rate on or before the report end date. The
report displays the rate's date. A missing or ambiguous latest rate stops the
report instead of showing unconverted amounts.

The separate Trial Balance (Simple) report is at
`http://127.0.0.1:8000/reports/trial-balance-simple/`. It follows ERPNext's
company-only query: uncancelled GL entries are grouped by fiscal year, posting
date, and account, with debit and credit totals and the group's maximum Finance
Book name. It shows all available dates and exports CSV. Access requires
`view_glentry`.

The basic Trial Balance for Party report is at
`http://127.0.0.1:8000/reports/trial-balance-for-party/`. It currently supports
Customers and Suppliers. Select a company, fiscal year, date range, and party
type; optionally select a party or account subtree. Opening entries are counted
in opening balances, period debit and credit stay separate, and closing balances
are netted per party. Zero-balance parties can be included, and the filtered
result can be downloaded as CSV. Access requires `view_glentry`.

The Voucher-wise Balance diagnostic report is at
`http://127.0.0.1:8000/reports/voucher-wise-balance/`. Select a company and
optionally a voucher type or posting-date range. It lists only voucher type
and number pairs whose uncancelled GL debits and credits differ, with a CSV
export. Newly posted Django vouchers must balance, so this mainly helps audit
historical imported ledger rows. Access requires `view_glentry`.

The basic Balance Sheet report is at
`http://127.0.0.1:8000/reports/balance-sheet/`. Select a company, fiscal year,
and date within that year. It shows account rows for assets, liabilities, and
equity, with provisional current profit or loss and any unclosed prior-year
profit or loss as separate balancing lines. Cost center, Project, and Finance
Book filters use the Trial Balance rules, including the default-book option.
An optional presentation currency uses the Trial Balance rate rule. The page
and CSV require `view_glentry`.

To compare accumulated balances at multiple dates, open
`http://127.0.0.1:8000/reports/balance-sheet/comparison/`. Choose a date range
inside one fiscal year and monthly, quarterly, half-yearly, or yearly periods.
The last column ends on the selected To Date. The comparison page also exports CSV.
Choose Growth view to keep the first period's amount and show percentage changes
in later columns; the CSV uses the same values.
The Values selector can show either accumulated balances or movement within
each period. Period movement headings show the exact start and end dates.

For year-end columns across consecutive fiscal years, open
`http://127.0.0.1:8000/reports/balance-sheet/yearly/`. Select a company and
first and last fiscal years. This view also supports Growth and CSV.

The basic Profit and Loss Statement is at
`http://127.0.0.1:8000/reports/profit-and-loss/`. Select a date range within
one fiscal year to see income, expense, and net profit or loss. Period closing
does not erase the report's historical activity. The page and CSV require
`view_glentry`.

Open `http://127.0.0.1:8000/reports/profit-and-loss/comparison/` to compare
monthly, quarterly, half-yearly, or yearly periods within one fiscal year.
Select period activity or accumulated values; the Total column follows the
selected mode. The comparison page also exports CSV.
Growth view shows change from the previous period; Margin view shows each value
as a percentage of Total Income. The Total column remains a currency amount.

For annual Profit and Loss columns across consecutive fiscal years, open
`http://127.0.0.1:8000/reports/profit-and-loss/yearly/`. This page supports
Report, Growth, and Margin views and CSV.

Create basic Projects in Django Admin with an ID, unique project name, company,
status, and optional customer and dates. A Journal Entry Account row may select
a Project from the same company; submission carries it to the GL row. Project
task planning, costing, billing, and the remaining ERPNext Project workflows
are still to be ported.

Item Prices now link to Customer and Supplier records. Existing prices with
text-only party names are preserved during migration. After importing the
matching party records, link any remaining names with:

```powershell
.venv\Scripts\python manage.py reconcile_item_price_parties
```

For addresses and contacts, create the Customer or Supplier and the Address
or Contact in Django Admin. Edit the Address or Contact to add a party link,
then edit the party to select its primary address or contact. A shared Address
or Contact can link to more than one party. Add Contact Email and Contact Phone
rows while editing the Contact. The Customer and Supplier forms also have
quick-entry fields for email, mobile, and address. Filling them creates linked
primary records when saving a party that does not already have them. City and
country are required when entering an address. Links to other ERPNext document
types remain open work. The party admin shows current email, mobile, contact
name, and a plain-text address from its primary linked records; edits to those
records appear on the next view.
