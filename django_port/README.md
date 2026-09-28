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
company-currency GL activity with cost centers. Foreign-currency profit-and-loss
and stock-account activity are refused until their closing workflows exist.
After submission, GL postings dated on or before the closing date are blocked
until cancellation and re-closing are implemented.

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
