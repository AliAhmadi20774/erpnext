import csv
import io
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from geo.models import Country, Currency
from organizations.models import Company
from parties.models import Customer, Supplier
from projects.models import Project

from .fiscal import create_fiscal_year
from .general_ledger_report import general_ledger_report
from .ledger import LedgerLine, post_gl_entries
from .models import Account, FinanceBook


class GeneralLedgerReportTests(TestCase):
    def setUp(self):
        usd = Currency.objects.create(name="USD", enabled=True)
        country = Country.objects.create(name="United States", code="US")
        self.company = Company.objects.create(name="Example", abbr="EX", country=country, default_currency=usd)
        self.other = Company.objects.create(name="Other", abbr="OT", country=country, default_currency=usd)
        create_fiscal_year(year="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31))
        self.assets = Account.objects.create(name="Assets - EX", account_name="Assets", company=self.company, root_type="Asset", is_group=True)
        self.bank = Account.objects.create(name="Bank - EX", account_name="Bank", company=self.company, parent_account=self.assets)
        self.cash = Account.objects.create(name="Cash - EX", account_name="Cash", company=self.company, parent_account=self.assets)
        other_root = Account.objects.create(name="Assets - OT", account_name="Assets", company=self.other, root_type="Asset", is_group=True)
        self.other_bank = Account.objects.create(name="Bank - OT", account_name="Bank", company=self.other, parent_account=other_root)
        self.default_book = FinanceBook.objects.create(finance_book_name="Statutory")
        self.other_book = FinanceBook.objects.create(finance_book_name="Management")
        self.company.default_finance_book = self.default_book
        self.company.save()

    def post(self, day, amount, number, book=None, opening=False, company=None, debit=None, credit=None):
        company = company or self.company
        post_gl_entries(
            company=company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
            is_opening=opening,
            lines=(
                LedgerLine(debit or self.bank, debit=amount, finance_book=book),
                LedgerLine(credit or self.cash, credit=amount, finance_book=book),
            ),
        )

    def test_finance_book_filters_and_opening_running_balance(self):
        self.post(date(2025, 1, 1), 100, "OPEN", opening=True)
        self.post(date(2025, 3, 15), 10, "UNASSIGNED")
        self.post(date(2025, 4, 1), 20, "A-1", book=self.default_book)
        self.post(date(2025, 4, 2), 30, "B-1", book=self.other_book)
        self.post(date(2025, 5, 1), 40, "A-2", book=self.default_book)
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 5, 31), account=self.bank)
        default = general_ledger_report(**options)
        self.assertEqual((default.opening_debit, default.period_debit, default.closing_debit),
                         (Decimal("110"), Decimal("60"), Decimal("170")))
        self.assertEqual([row.running_balance for row in default.rows], [Decimal("130"), Decimal("170")])
        other = general_ledger_report(**options, finance_book=self.other_book, include_default_book_entries=False)
        self.assertEqual((other.opening_debit, other.period_debit, other.closing_debit),
                         (Decimal("110"), Decimal("30"), Decimal("140")))
        unassigned = general_ledger_report(**options, include_default_book_entries=False)
        self.assertEqual((unassigned.opening_debit, unassigned.period_debit), (Decimal("110"), Decimal("0")))
        with self.assertRaises(ValidationError):
            general_ledger_report(**options, finance_book=self.other_book, include_default_book_entries=True)

    def test_disable_opening_calculation_keeps_explicit_openings_only(self):
        self.post(date(2025, 1, 1), 100, "YEAR-OPEN", opening=True)
        self.post(date(2025, 3, 1), 10, "OLD-ORDINARY")
        self.post(date(2025, 4, 1), 20, "PERIOD-OPEN", opening=True)
        self.post(date(2025, 4, 2), 5, "CURRENT")
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=self.bank)
        normal = general_ledger_report(**options)
        self.assertEqual((normal.opening_debit, normal.period_debit, normal.rows[0].running_balance),
                         (Decimal("130"), Decimal("5"), Decimal("135")))
        disabled = general_ledger_report(**options, disable_opening_balance_calculation=True)
        self.assertEqual((disabled.opening_debit, disabled.period_debit, disabled.closing_debit),
                         (Decimal("100"), Decimal("25"), Decimal("125")))
        self.assertEqual([row.entry.voucher_no for row in disabled.rows], ["PERIOD-OPEN", "CURRENT"])
        self.assertEqual([row.running_balance for row in disabled.rows], [Decimal("120"), Decimal("125")])
        shown = general_ledger_report(**options, show_opening_entries=True)
        self.assertEqual((shown.opening_debit, shown.period_debit), (Decimal("110"), Decimal("25")))
        both = general_ledger_report(**options, disable_opening_balance_calculation=True, show_opening_entries=True)
        self.assertEqual((both.opening_debit, both.period_debit), (Decimal("100"), Decimal("25")))
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": self.bank.pk, "disable_opening_balance_calculation": "on"}
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual([row[3] for row in rows[2:-2]], ["PERIOD-OPEN", "CURRENT"])
        self.assertEqual((Decimal(rows[1][10]), Decimal(rows[-1][10])), (Decimal("100"), Decimal("125")))

    def test_optional_remarks_are_escaped_in_html_and_safe_in_csv(self):
        post_gl_entries(company=self.company, posting_date=date(2025, 4, 1),
                        voucher_type="Journal Entry", voucher_no="JE-REMARK", lines=(
                            LedgerLine(self.bank, debit=10, remarks="=1+1 <script>"),
                            LedgerLine(self.cash, credit=10, remarks="Offset"),
                        ))
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        url = reverse("general_ledger_report")
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": self.bank.pk}
        hidden = self.client.get(url, params)
        self.assertNotContains(hidden, "<script>")
        self.assertNotContains(hidden, "&lt;script&gt;")
        page = self.client.get(url, params | {"show_remarks": "on"})
        self.assertContains(page, "&lt;script&gt;")
        self.assertNotContains(page, "<script>")
        hidden_csv = self.client.get(url, params | {"format": "csv"})
        hidden_rows = list(csv.reader(io.StringIO(hidden_csv.content.decode("utf-8"))))
        self.assertNotIn("Remarks", hidden_rows[0])
        response = self.client.get(url, params | {"show_remarks": "on", "format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual((rows[0][-1], rows[2][-1]), ("Remarks", "'=1+1 <script>"))
        grouped = self.client.get(url, {"company": self.company.pk, "from_date": "2025-04-01",
                                        "to_date": "2025-04-30", "group_by_voucher": "on",
                                        "show_remarks": "on", "format": "csv"})
        grouped_rows = list(csv.reader(io.StringIO(grouped.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in grouped_rows}), 1)

    def test_opening_display_account_tree_and_company_scope(self):
        self.post(date(2025, 1, 1), 50, "OPEN", opening=True)
        options = dict(company=self.company, from_date=date(2025, 1, 1), to_date=date(2025, 1, 31), account=self.bank)
        hidden = general_ledger_report(**options)
        self.assertEqual((hidden.opening_debit, hidden.period_debit, len(hidden.rows)), (Decimal("50"), Decimal("0"), 0))
        shown = general_ledger_report(**options, show_opening_entries=True)
        self.assertEqual((shown.opening_debit, shown.period_debit, len(shown.rows)), (Decimal("0"), Decimal("50"), 1))
        whole_tree = general_ledger_report(
            company=self.company, from_date=date(2025, 1, 1), to_date=date(2025, 1, 31),
            account=self.assets, show_opening_entries=True,
        )
        self.assertEqual(len(whole_tree.rows), 2)
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"account": self.other_bank}))
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"from_date": date(2025, 2, 1)}))

    def test_party_and_exact_voucher_filters_apply_to_totals_and_rows(self):
        receivable = Account.objects.create(name="Receivable - EX", account_name="Receivable", company=self.company,
                                             parent_account=self.assets, account_type="Receivable")
        alice = Customer.objects.create(name="Alice", customer_name="Alice")
        bob = Customer.objects.create(name="Bob", customer_name="Bob")
        for day, amount, number, customer in (
            (date(2025, 3, 1), 5, "OLD", alice),
            (date(2025, 4, 1), 10, "A-1", alice),
            (date(2025, 4, 2), 20, "A-10", alice),
            (date(2025, 4, 3), 30, "B-1", bob),
        ):
            post_gl_entries(company=self.company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
                            lines=(LedgerLine(receivable, debit=amount, customer=customer), LedgerLine(self.bank, credit=amount)))
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=receivable, party_type="Customer", party=alice.pk)
        result = general_ledger_report(**options)
        self.assertEqual((result.opening_debit, result.period_debit, result.closing_debit),
                         (Decimal("5"), Decimal("30"), Decimal("35")))
        self.assertEqual([row.entry.voucher_no for row in result.rows], ["A-1", "A-10"])
        exact = general_ledger_report(**options, voucher_no="A-1")
        self.assertEqual((exact.opening_debit, exact.period_debit, len(exact.rows)),
                         (Decimal("0"), Decimal("10"), 1))
        supplier = Supplier.objects.create(name="Vendor", supplier_name="Vendor")
        self.assertEqual(general_ledger_report(**(options | {"party_type": "Supplier", "party": supplier.pk})).rows, ())
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"party_type": ""}))

    def test_reference_voucher_filter_is_exact_and_keeps_balances_consistent(self):
        for day, number, reference, amount in (
            (date(2025, 3, 1), "JE-OLD", "SI-1", 5),
            (date(2025, 4, 1), "JE-1", "SI-1", 10),
            (date(2025, 4, 2), "JE-2", "SI-10", 20),
        ):
            post_gl_entries(company=self.company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
                            lines=(LedgerLine(self.bank, debit=amount, against_voucher_type="Sales Invoice",
                                              against_voucher=reference), LedgerLine(self.cash, credit=amount)))
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=self.bank, against_voucher_no="SI-1")
        result = general_ledger_report(**options)
        self.assertEqual((result.opening_debit, result.period_debit, result.closing_debit),
                         (Decimal("5"), Decimal("10"), Decimal("15")))
        self.assertEqual([row.entry.voucher_no for row in result.rows], ["JE-1"])
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": self.bank.pk, "against_voucher_no": "SI-1", "format": "csv"}
        response = self.client.get(reverse("general_ledger_report"), params)
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual((rows[2][3], rows[2][4], rows[2][5]), ("JE-1", "Sales Invoice", "SI-1"))
        self.assertEqual(Decimal(rows[3][10]), Decimal("10"))

    def test_single_foreign_account_has_separate_currency_balances_and_export(self):
        eur = Currency.objects.create(name="EUR", enabled=True)
        euro_bank = Account.objects.create(name="Euro Bank - EX", account_name="Euro Bank", company=self.company,
                                           parent_account=self.assets, account_currency=eur)
        for day, number, amount, rate, company_amount in (
            (date(2025, 3, 1), "JE-OLD", 10, "1.2", 12),
            (date(2025, 4, 1), "JE-NEW", 20, "1.3", 26),
        ):
            post_gl_entries(company=self.company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
                            lines=(LedgerLine(euro_bank, debit_in_account_currency=Decimal(amount),
                                              exchange_rate=Decimal(rate)), LedgerLine(self.bank, credit=company_amount)))
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=euro_bank, print_in_account_currency=True)
        result = general_ledger_report(**options)
        self.assertEqual(result.account_currency, "EUR")
        self.assertEqual((result.opening_debit, result.period_debit, result.closing_debit),
                         (Decimal("12"), Decimal("26"), Decimal("38")))
        self.assertEqual((result.opening_debit_in_account_currency, result.period_debit_in_account_currency,
                          result.closing_debit_in_account_currency, result.rows[0].running_balance_in_account_currency),
                         (Decimal("10"), Decimal("20"), Decimal("30"), Decimal("30")))
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"account": None}))
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"account": self.assets}))
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": euro_bank.pk, "print_in_account_currency": "on"}
        page = self.client.get(reverse("general_ledger_report"), params)
        self.assertContains(page, "Debit (EUR)")
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual((rows[0][13], rows[2][13]), ("Account Currency", "EUR"))
        self.assertEqual((Decimal(rows[1][14]), Decimal(rows[2][14]), Decimal(rows[2][16])),
                         (Decimal("10"), Decimal("20"), Decimal("30")))

    def test_group_by_account_has_opening_period_closing_per_account(self):
        self.post(date(2025, 3, 1), 40, "OLD")
        self.post(date(2025, 4, 1), 10, "A-1")
        self.post(date(2025, 4, 2), 5, "A-2", debit=self.cash, credit=self.bank)
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=self.assets, group_by_account=True)
        result = general_ledger_report(**options)
        self.assertEqual((result.opening_debit, result.opening_credit, result.period_debit, result.period_credit),
                         (Decimal("40"), Decimal("40"), Decimal("15"), Decimal("15")))
        groups = {group.account.pk: group for group in result.groups}
        bank = groups[self.bank.pk]
        cash = groups[self.cash.pk]
        self.assertEqual((bank.opening_debit, bank.period_debit, bank.period_credit, bank.closing_debit, bank.closing_credit),
                         (Decimal("40"), Decimal("10"), Decimal("5"), Decimal("50"), Decimal("5")))
        self.assertEqual((cash.opening_credit, cash.period_debit, cash.period_credit, cash.closing_credit),
                         (Decimal("40"), Decimal("5"), Decimal("10"), Decimal("50")))
        self.assertEqual([row.running_balance for row in bank.rows], [Decimal("50"), Decimal("45")])
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"account": self.bank}))
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": self.assets.pk, "group_by_account": "on"}
        page = self.client.get(reverse("general_ledger_report"), params)
        self.assertContains(page, "Bank - EX")
        self.assertContains(page, "Cash - EX")
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual([(row[0], row[1]) for row in rows if row[0] == "Account group"],
                         [("Account group", self.bank.pk), ("Account group", self.cash.pk)])
        self.assertEqual([(row[1], Decimal(row[10])) for row in rows if row[0] == "Period total" and row[1]],
                         [(self.bank.pk, Decimal("10")), (self.cash.pk, Decimal("5"))])

    def test_project_filter_scopes_opening_period_and_csv(self):
        first = Project.objects.create(name="PROJ-1", project_name="First", company=self.company)
        second = Project.objects.create(name="PROJ-2", project_name="Second", company=self.company)
        foreign = Project.objects.create(name="PROJ-OT", project_name="Foreign", company=self.other)
        for day, amount, number, project in (
            (date(2025, 3, 1), 5, "OLD", first),
            (date(2025, 4, 1), 10, "FIRST", first),
            (date(2025, 4, 2), 20, "SECOND", second),
        ):
            post_gl_entries(company=self.company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
                            lines=(LedgerLine(self.bank, debit=amount, project=project),
                                   LedgerLine(self.cash, credit=amount)))
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=self.bank, project=first)
        result = general_ledger_report(**options)
        self.assertEqual((result.opening_debit, result.period_debit, result.closing_debit),
                         (Decimal("5"), Decimal("10"), Decimal("15")))
        self.assertEqual([row.entry.voucher_no for row in result.rows], ["FIRST"])
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"project": foreign}))
        with self.assertRaises(ValidationError):
            post_gl_entries(company=self.company, posting_date=date(2025, 4, 3), voucher_type="Journal Entry",
                            voucher_no="BAD", lines=(LedgerLine(self.bank, debit=1, project=foreign),
                                                    LedgerLine(self.cash, credit=1)))
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": self.bank.pk, "project": first.pk}
        page = self.client.get(reverse("general_ledger_report"), params)
        self.assertContains(page, "PROJ-1")
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual((rows[0][-1], rows[2][-1], rows[2][3]), ("Project", "PROJ-1", "FIRST"))

    def test_group_by_party_separates_party_types_and_unassigned_rows(self):
        receivable = Account.objects.create(name="Receivable - EX", account_name="Receivable", company=self.company,
                                             parent_account=self.assets, account_type="Receivable")
        liabilities = Account.objects.create(name="Liabilities - EX", account_name="Liabilities", company=self.company,
                                              root_type="Liability", is_group=True)
        payable = Account.objects.create(name="Payable - EX", account_name="Payable", company=self.company,
                                          parent_account=liabilities, account_type="Payable")
        customer = Customer.objects.create(name="Shared", customer_name="Shared Customer")
        supplier = Supplier.objects.create(name="Shared", supplier_name="Shared Supplier")
        for day, number, lines in (
            (date(2025, 3, 1), "OLD", (LedgerLine(receivable, debit=5, customer=customer), LedgerLine(self.bank, credit=5))),
            (date(2025, 4, 1), "SALE", (LedgerLine(receivable, debit=10, customer=customer), LedgerLine(self.bank, credit=10))),
            (date(2025, 4, 2), "BILL", (LedgerLine(payable, credit=7, supplier=supplier), LedgerLine(self.bank, debit=7))),
            (date(2025, 4, 3), "PAYMENT", (LedgerLine(receivable, credit=2, customer=customer), LedgerLine(self.bank, debit=2))),
        ):
            post_gl_entries(company=self.company, posting_date=day, voucher_type="Journal Entry", voucher_no=number,
                            lines=lines)
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       group_by_party=True)
        result = general_ledger_report(**options)
        groups = {(group.party_type, group.party): group for group in result.party_groups}
        self.assertEqual(set(groups), {("Customer", "Shared"), ("Supplier", "Shared"), ("No party", "")})
        customer_group = groups[("Customer", "Shared")]
        self.assertEqual((customer_group.opening_debit, customer_group.period_debit, customer_group.period_credit,
                          customer_group.closing_debit, customer_group.closing_credit),
                         (Decimal("5"), Decimal("10"), Decimal("2"), Decimal("15"), Decimal("2")))
        self.assertEqual([row.running_balance_in_group for row in customer_group.rows],
                         [Decimal("15"), Decimal("13")])
        self.assertEqual(groups[("Supplier", "Shared")].period_credit, Decimal("7"))
        self.assertEqual(groups[("No party", "")].opening_credit, Decimal("5"))
        with self.assertRaises(ValidationError):
            general_ledger_report(**options, group_by_account=True)
        with self.assertRaises(ValidationError):
            general_ledger_report(**options, print_in_account_currency=True, account=receivable)
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "group_by_party": "on"}
        page = self.client.get(reverse("general_ledger_report"), params)
        self.assertContains(page, "Party balance")
        self.assertContains(page, "No party")
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual(rows[0][12], "Party Balance")
        self.assertEqual({(row[6], row[7]) for row in rows if row[0] == "Party group"},
                         {("Customer", "Shared"), ("Supplier", "Shared"), ("No party", "")})

    def test_group_by_voucher_keeps_types_distinct_and_matches_totals(self):
        self.post(date(2025, 3, 1), 5, "OLD")
        for day, voucher_type, amount in (
            (date(2025, 4, 1), "Journal Entry", 10),
            (date(2025, 4, 2), "Payment Entry", 20),
        ):
            post_gl_entries(company=self.company, posting_date=day, voucher_type=voucher_type, voucher_no="SHARED",
                            lines=(LedgerLine(self.bank, debit=amount), LedgerLine(self.cash, credit=amount)))
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       group_by_voucher=True)
        result = general_ledger_report(**options)
        self.assertEqual((result.opening_debit, result.opening_credit, result.period_debit, result.period_credit),
                         (Decimal("5"), Decimal("5"), Decimal("30"), Decimal("30")))
        groups = {(group.voucher_type, group.voucher_no): group for group in result.voucher_groups}
        self.assertEqual(set(groups), {("Journal Entry", "SHARED"), ("Payment Entry", "SHARED")})
        self.assertEqual((groups[("Journal Entry", "SHARED")].period_debit,
                          groups[("Payment Entry", "SHARED")].period_credit), (Decimal("10"), Decimal("20")))
        self.assertEqual(groups[("Journal Entry", "SHARED")].rows[-1].running_balance_in_group, Decimal("0"))
        with self.assertRaises(ValidationError):
            general_ledger_report(**(options | {"voucher_no": "SHARED"}))
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "group_by_voucher": "on"}
        page = self.client.get(reverse("general_ledger_report"), params)
        self.assertContains(page, "Voucher balance")
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual(rows[0][12], "Voucher Balance")
        self.assertEqual({(row[2], row[3]) for row in rows if row[0] == "Voucher group"},
                         {("Journal Entry", "SHARED"), ("Payment Entry", "SHARED")})

    def test_consolidated_voucher_rows_preserve_dimensions_and_balances(self):
        first = Project.objects.create(name="PROJ-1", project_name="First", company=self.company)
        second = Project.objects.create(name="PROJ-2", project_name="Second", company=self.company)
        self.post(date(2025, 3, 1), 4, "OLD")
        post_gl_entries(company=self.company, posting_date=date(2025, 4, 1),
                        voucher_type="Journal Entry", voucher_no="JE-1", lines=(
                            LedgerLine(self.bank, debit=5, project=first),
                            LedgerLine(self.bank, debit=7, project=first),
                            LedgerLine(self.bank, debit=3, project=second),
                            LedgerLine(self.cash, credit=15),
                        ))
        options = dict(company=self.company, from_date=date(2025, 4, 1), to_date=date(2025, 4, 30),
                       account=self.assets)
        self.assertEqual(len(general_ledger_report(**options).rows), 4)
        result = general_ledger_report(**options, consolidate_vouchers=True)
        self.assertEqual(len(result.rows), 3)
        self.assertEqual((result.opening_debit, result.period_debit, result.period_credit, result.closing_debit),
                         (Decimal("4"), Decimal("15"), Decimal("15"), Decimal("19")))
        project_rows = {row.entry.project_id: row for row in result.rows if row.entry.account_id == self.bank.pk}
        self.assertEqual((project_rows[first.pk].debit, project_rows[first.pk].source_count,
                          project_rows[first.pk].running_balance), (Decimal("12"), 2, Decimal("16")))
        self.assertEqual((project_rows[second.pk].debit, project_rows[second.pk].running_balance),
                         (Decimal("3"), Decimal("19")))
        filtered = general_ledger_report(**options, consolidate_vouchers=True, voucher_no="JE-1")
        self.assertEqual((filtered.opening_debit, filtered.period_debit), (Decimal("0"), Decimal("15")))
        currency = general_ledger_report(**(options | {"account": self.bank}),
                                         consolidate_vouchers=True, print_in_account_currency=True)
        self.assertEqual((currency.period_debit_in_account_currency, currency.rows[0].debit_in_account_currency),
                         (Decimal("15"), Decimal("12")))
        with self.assertRaises(ValidationError):
            general_ledger_report(**options, consolidate_vouchers=True, group_by_account=True)
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        params = {"company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
                  "account": self.assets.pk, "consolidate_vouchers": "on"}
        page = self.client.get(reverse("general_ledger_report"), params)
        self.assertEqual(len(page.context["report"].rows), 3)
        response = self.client.get(reverse("general_ledger_report"), params | {"format": "csv"})
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8"))))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertEqual([(row[-1], Decimal(row[10])) for row in rows[2:-2] if row[1] == self.bank.pk],
                         [(first.pk, Decimal("12")), (second.pk, Decimal("3"))])

    def test_read_only_page_requires_ledger_view_permission(self):
        self.post(date(2025, 4, 1), 20, "A-1", book=self.default_book)
        url = reverse("general_ledger_report")
        params = {
            "company": self.company.pk, "from_date": "2025-04-01", "to_date": "2025-04-30",
            "account": self.bank.pk, "include_default_book_entries": "on",
        }
        self.assertEqual(self.client.get(url, params).status_code, 302)
        user = get_user_model().objects.create_user(username="regular", password="test-password")
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, params).status_code, 403)
        admin = get_user_model().objects.create_superuser(username="admin", password="test-password", email="admin@example.com")
        self.client.force_login(admin)
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["report"].period_debit, Decimal("20"))
        self.assertContains(response, "A-1")
        invalid = self.client.get(url, params | {"finance_book": self.other_book.pk})
        self.assertEqual(invalid.status_code, 200)
        self.assertIsNone(invalid.context["report"])
        self.assertTrue(invalid.context["form"].non_field_errors())
        csv_response = self.client.get(url, params | {"format": "csv", "voucher_no": "A-1"})
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response["Content-Type"])
        rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8"))))
        self.assertEqual([row[0] for row in rows], ["Date", "Opening", "2025-04-01", "Period total", "Closing"])
        self.assertEqual(rows[2][3], "A-1")
        self.assertEqual(Decimal(rows[3][10]), Decimal("20"))
        self.client.force_login(user)
        self.assertEqual(self.client.get(url, params | {"format": "csv"}).status_code, 403)
