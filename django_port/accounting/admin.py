from django.contrib import admin
from django.contrib import messages
from django.core.exceptions import ValidationError

from .closing import submit_period_closing_voucher
from .journal import submit_journal_entry
from .models import Account, AccountClosingBalance, AccountingPeriod, ClosedDocument, CostCenter, FinanceBook, FiscalYear, FiscalYearCompany, GLEntry, JournalEntry, JournalEntryAccount, PartyAccount, PeriodClosingVoucher
from .periods import PERIOD_CLOSING_DOCUMENT_TYPES


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "account_name", "company", "parent_account", "is_group", "account_type", "account_currency", "lft", "rgt", "disabled")
    list_filter = ("company", "root_type", "is_group", "disabled")
    search_fields = ("name", "account_name", "account_number")
    readonly_fields = ("lft", "rgt")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["name", "company"] if obj else [])

    def delete_queryset(self, request, queryset):
        for account in queryset.order_by("-lft"):
            account.delete()


@admin.register(PartyAccount)
class PartyAccountAdmin(admin.ModelAdmin):
    list_display = ("customer", "supplier", "customer_group", "supplier_group", "company", "account", "advance_account")
    list_filter = ("company",)
    search_fields = ("customer__name", "supplier__name", "customer_group__name", "supplier_group__name", "account__name")


@admin.register(CostCenter)
class CostCenterAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "parent_cost_center", "is_group", "disabled", "lft", "rgt")
    list_filter = ("company", "is_group", "disabled")
    search_fields = ("name", "cost_center_name", "cost_center_number")
    readonly_fields = ("lft", "rgt")

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["name", "company"] if obj else [])

    def delete_queryset(self, request, queryset):
        for cost_center in queryset.order_by("-lft"):
            cost_center.delete()


@admin.register(FinanceBook)
class FinanceBookAdmin(admin.ModelAdmin):
    list_display = ("name", "finance_book_name")
    search_fields = ("name", "finance_book_name")
    exclude = ("name",)

    def get_readonly_fields(self, request, obj=None):
        return ["finance_book_name"] if obj else []


@admin.register(FiscalYear)
class FiscalYearAdmin(admin.ModelAdmin):
    list_display = ("year", "year_start_date", "year_end_date", "all_companies", "is_short_year", "disabled")
    list_filter = ("all_companies", "is_short_year", "disabled")
    search_fields = ("year",)
    readonly_fields = ("all_companies", "auto_created")
    exclude = ("companies",)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["year", "year_start_date", "year_end_date", "is_short_year"] if obj else [])


@admin.register(FiscalYearCompany)
class FiscalYearCompanyAdmin(admin.ModelAdmin):
    list_display = ("fiscal_year", "company")
    list_filter = ("company",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ClosedDocumentInline(admin.TabularInline):
    model = ClosedDocument
    extra = 0
    fields = ("document_type", "closed")

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "document_type":
            kwargs["choices"] = [(name, name) for name in PERIOD_CLOSING_DOCUMENT_TYPES]
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(AccountingPeriod)
class AccountingPeriodAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "start_date", "end_date", "disabled", "exempted_role")
    list_filter = ("company", "disabled")
    search_fields = ("name", "period_name")
    exclude = ("name",)
    inlines = (ClosedDocumentInline,)

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        return fields + (["period_name", "company"] if obj else [])

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        if not change and not form.instance.closed_documents.exists():
            ClosedDocument.objects.bulk_create([
                ClosedDocument(accounting_period=form.instance, document_type=name, closed=True)
                for name in PERIOD_CLOSING_DOCUMENT_TYPES
            ])


@admin.register(PeriodClosingVoucher)
class PeriodClosingVoucherAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "fiscal_year", "period_start_date", "period_end_date", "closing_account_head", "status")
    list_filter = ("company", "fiscal_year", "status")
    search_fields = ("name", "remarks")
    actions = ("submit_selected",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.status == PeriodClosingVoucher.Status.DRAFT)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or obj.status == PeriodClosingVoucher.Status.DRAFT)

    def delete_queryset(self, request, queryset):
        for voucher in queryset:
            voucher.delete()

    @admin.action(description="Submit selected period closing vouchers")
    def submit_selected(self, request, queryset):
        for voucher in queryset.order_by("company", "period_end_date"):
            try:
                submit_period_closing_voucher(voucher, user=request.user)
            except ValidationError as exc:
                self.message_user(request, f"{voucher.name}: {exc}", level=messages.ERROR)
            else:
                self.message_user(request, f"Submitted {voucher.name}.", level=messages.SUCCESS)


@admin.register(AccountClosingBalance)
class AccountClosingBalanceAdmin(admin.ModelAdmin):
    list_display = ("closing_date", "company", "account", "cost_center", "finance_book", "project", "debit", "credit", "is_period_closing_voucher_entry", "period_closing_voucher")
    list_filter = ("company", "finance_book", "project", "closing_date", "is_period_closing_voucher_entry")
    search_fields = ("account__name", "period_closing_voucher__name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class JournalEntryAccountInline(admin.TabularInline):
    model = JournalEntryAccount
    extra = 2
    fields = (
        "position", "account", "cost_center", "project", "customer", "supplier",
        "reference_type", "reference_name",
        "debit_in_account_currency", "credit_in_account_currency", "exchange_rate", "user_remark",
    )


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("name", "posting_date", "company", "finance_book", "voucher_type", "total_debit", "total_credit", "status")
    list_filter = ("company", "finance_book", "voucher_type", "status", "posting_date")
    search_fields = ("name", "remark")
    inlines = (JournalEntryAccountInline,)
    actions = ("submit_selected",)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.status == JournalEntry.Status.DRAFT)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or obj.status == JournalEntry.Status.DRAFT)

    def delete_queryset(self, request, queryset):
        for journal in queryset:
            journal.delete()

    @admin.action(description="Submit selected journal entries")
    def submit_selected(self, request, queryset):
        for journal in queryset.order_by("company", "posting_date", "name"):
            try:
                submit_journal_entry(journal, user=request.user)
            except ValidationError as exc:
                self.message_user(request, f"{journal.name}: {exc}", level=messages.ERROR)
            else:
                self.message_user(request, f"Submitted {journal.name}.", level=messages.SUCCESS)


@admin.register(GLEntry)
class GLEntryAdmin(admin.ModelAdmin):
    list_display = ("posting_date", "fiscal_year", "company", "account", "cost_center", "project", "finance_book", "debit", "credit", "account_currency", "account_exchange_rate", "voucher_type", "voucher_no", "customer", "supplier")
    list_filter = ("company", "finance_book", "fiscal_year", "posting_date", "voucher_type", "is_opening")
    search_fields = ("account__name", "voucher_no", "customer__name", "supplier__name")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
