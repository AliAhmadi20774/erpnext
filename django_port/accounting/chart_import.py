"""Read ERPNext chart templates as data and install an untouched chart for a company."""

import ast
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction

from geo.models import Currency
from organizations.models import Company

from .models import Account


CHART_DIRECTORY = Path(settings.BASE_DIR).parent / "erpnext" / "accounts" / "doctype" / "account" / "chart_of_accounts" / "verified"
STANDARD_TEMPLATES = {
    "Standard": "standard_chart_of_accounts.py",
    "Standard with Numbers": "standard_chart_of_accounts_with_account_number.py",
}
METADATA = frozenset((
    "account_name", "account_number", "account_type", "account_category",
    "root_type", "is_group", "tax_rate", "account_currency",
))

_VERIFIED_TEMPLATES_CACHE = None


def get_verified_chart_templates():
    """Return a mapping of template name, stem, and filename to verified chart JSON path."""
    global _VERIFIED_TEMPLATES_CACHE
    if _VERIFIED_TEMPLATES_CACHE is None:
        cache = {}
        if CHART_DIRECTORY.is_dir():
            for f in CHART_DIRECTORY.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                name = data.get("name")
                if name:
                    cache[name] = f
                cache[f.stem] = f
                cache[f.name] = f
        _VERIFIED_TEMPLATES_CACHE = cache
    return _VERIFIED_TEMPLATES_CACHE


def get_charts_for_country(country, with_standard=True):
    """Return available chart templates matching a Country (model, code, or name)."""
    country_code = ""
    country_name = ""
    if hasattr(country, "code"):
        country_code = country.code or ""
        country_name = country.name or ""
    elif isinstance(country, str):
        if len(country) <= 3:
            country_code = country
        else:
            country_name = country
            from geo.models import Country
            c_obj = Country.objects.filter(name__iexact=country).first()
            if c_obj:
                country_code = c_obj.code or ""

    charts = []
    cc = country_code.strip().lower()
    cn = country_name.strip().lower()
    if CHART_DIRECTORY.is_dir():
        for f in sorted(CHART_DIRECTORY.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("disabled", "No") == "Yes":
                continue
            c_code = (data.get("country_code") or "").strip().lower()
            name = data.get("name")
            if not name:
                continue
            if cc and (c_code == cc or f.name.lower().startswith(cc)):
                if name not in charts:
                    charts.append(name)
            elif cn and f.name.lower().startswith(cn):
                if name not in charts:
                    charts.append(name)

    if len(charts) != 1 or with_standard:
        charts.extend(["Standard", "Standard with Numbers"])
    return charts


def validate_chart_template(template):
    """Return True if template is a supported standard or verified chart template."""
    if template in STANDARD_TEMPLATES:
        return True
    verified = get_verified_chart_templates()
    return template in verified


def get_account_tree_from_existing_company(existing_company):
    """Extract nested chart-of-accounts tree from an existing company's accounts."""
    from collections import defaultdict

    if hasattr(existing_company, "pk"):
        company_id = existing_company.pk
        default_currency_id = existing_company.default_currency_id
    else:
        from organizations.models import Company
        comp = Company.objects.filter(pk=existing_company).first()
        company_id = existing_company
        default_currency_id = comp.default_currency_id if comp else None

    accounts = list(Account.objects.filter(company_id=company_id).order_by("lft", "rgt"))
    if not accounts:
        return {}

    children_by_parent = defaultdict(list)
    for acc in accounts:
        children_by_parent[acc.parent_account_id].append(acc)

    tree = {}

    def build_subtree(parent_id, current_tree):
        for child in children_by_parent.get(parent_id, []):
            node = {
                "account_name": child.account_name,
                "account_type": child.account_type or "",
                "is_group": child.is_group,
                "root_type": child.root_type,
                "account_number": child.account_number or "",
            }
            if child.tax_rate is not None:
                node["tax_rate"] = child.tax_rate
            if child.account_currency_id and child.account_currency_id != default_currency_id:
                node["account_currency"] = child.account_currency_id
            current_tree[child.account_name] = node
            build_subtree(child.pk, node)

    build_subtree(None, tree)
    return tree


class _IdentityTranslation(ast.NodeTransformer):
    def visit_Call(self, node):
        if not (
            isinstance(node.func, ast.Name) and node.func.id == "_"
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            raise ValueError("Standard chart contains an unsupported expression.")
        return node.args[0]


def load_chart(*, template="Standard", source=None, existing_company=None):
    if source is not None:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        tree = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(tree, dict):
            raise ValueError("Chart JSON must contain a tree object.")
        return tree
    if existing_company is not None:
        return get_account_tree_from_existing_company(existing_company)
    if template in STANDARD_TEMPLATES:
        module = ast.parse((CHART_DIRECTORY / STANDARD_TEMPLATES[template]).read_text(encoding="utf-8"))
        function = next((node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "get"), None)
        if function is None:
            raise ValueError("Standard chart has no get function.")
        result = next((node.value for node in function.body if isinstance(node, ast.Return)), None)
        if result is None:
            raise ValueError("Standard chart has no return value.")
        return ast.literal_eval(_IdentityTranslation().visit(result))

    verified = get_verified_chart_templates()
    if template in verified:
        path = verified[template]
        data = json.loads(path.read_text(encoding="utf-8"))
        tree = data.get("tree")
        if not isinstance(tree, dict):
            raise ValueError(f"Chart template {template!r} does not contain a tree object.")
        return tree

    raise ValueError(f"Unknown chart template: {template}")


@dataclass(frozen=True)
class ChartAccount:
    name: str
    account_name: str
    account_number: str
    parent_name: str | None
    is_group: bool
    root_type: str
    report_type: str
    account_type: str
    currency_id: str


def plan_chart(tree, company):
    if not isinstance(tree, dict) or not tree:
        raise ValueError("Chart tree must be a nonempty object.")
    plan = []
    occurrences = Counter()
    names = set()
    numbers = set()

    def visit(children, parent_name=None, root_type=None):
        for label, child in children.items():
            if label in METADATA:
                continue
            if not isinstance(label, str) or not isinstance(child, dict):
                raise ValueError("Each chart account must have a name and an object definition.")
            if parent_name is None:
                root_type = child.get("root_type")
                if root_type not in Account.RootType.values:
                    raise ValueError(f"Root account {label!r} needs a valid root_type.")
            account_name = child.get("account_name") or label
            if not isinstance(account_name, str) or not account_name.strip():
                raise ValueError(f"Account {label!r} has an invalid account name.")
            account_name = account_name.strip()
            account_number = str(child.get("account_number") or "").strip()
            key = (account_number.casefold(), account_name.casefold())
            suffix = occurrences[key]
            occurrences[key] += 1
            if suffix:
                account_name = f"{account_name} {suffix}"
            name = " - ".join(part for part in (account_number, account_name, company.abbr) if part)
            if len(name) > 140 or len(account_name) > 140:
                raise ValueError(f"Account {label!r} exceeds the 140-character name limit.")
            if name in names:
                raise ValueError(f"Duplicate account name: {name}")
            if account_number and account_number in numbers:
                raise ValueError(f"Duplicate account number: {account_number}")
            names.add(name)
            numbers.add(account_number)
            nested = {key: value for key, value in child.items() if key not in METADATA}
            is_group = bool(child.get("is_group") or nested)
            if parent_name is None and not is_group:
                raise ValueError(f"Root account {label!r} must be a group.")
            currency_id = child.get("account_currency") or company.default_currency_id
            if not isinstance(currency_id, str):
                raise ValueError(f"Account {label!r} has an invalid currency.")
            report_type = "Balance Sheet" if root_type in ("Asset", "Liability", "Equity") else "Profit and Loss"
            plan.append(ChartAccount(
                name=name, account_name=account_name, account_number=account_number,
                parent_name=parent_name, is_group=is_group, root_type=root_type,
                report_type=report_type, account_type=child.get("account_type") or "",
                currency_id=currency_id,
            ))
            visit(nested, name, root_type)

    visit(tree)
    return plan


@transaction.atomic
def install_chart(company, plan):
    company_row = Company.objects.select_for_update().get(pk=company.pk)
    expected = {row.name: row for row in plan}
    existing = list(Account.objects.filter(company=company_row))
    if existing:
        if len(existing) == len(expected) and all(
            account.name in expected
            and account.account_name == expected[account.name].account_name
            and account.account_number == expected[account.name].account_number
            and account.parent_account_id == expected[account.name].parent_name
            and account.is_group == expected[account.name].is_group
            and account.root_type == expected[account.name].root_type
            and account.report_type == expected[account.name].report_type
            and account.account_type == expected[account.name].account_type
            and account.account_currency_id == expected[account.name].currency_id
            for account in existing
        ):
            return 0
        raise ValueError("Company already has accounts that differ from this chart; no changes were made.")

    currency_ids = {row.currency_id for row in plan}
    missing = currency_ids - set(Currency.objects.filter(pk__in=currency_ids).values_list("pk", flat=True))
    if missing:
        raise ValueError(f"Create these currencies before importing the chart: {', '.join(sorted(missing))}")
    for row in plan:
        Account.objects.create(
            name=row.name, account_name=row.account_name, account_number=row.account_number,
            company=company_row, parent_account_id=row.parent_name, is_group=row.is_group,
            root_type=row.root_type, report_type=row.report_type, account_type=row.account_type,
            account_currency_id=row.currency_id,
        )
    changes = []
    for field, account_type in (("default_receivable_account", "Receivable"), ("default_payable_account", "Payable")):
        if not getattr(company_row, f"{field}_id"):
            account = Account.objects.filter(company=company_row, account_type=account_type, is_group=False, disabled=False).order_by("lft").first()
            if account:
                setattr(company_row, field, account)
                changes.append(field)
    if company_row.enable_perpetual_inventory:
        if not company_row.default_inventory_account_id:
            inv_acc = Account.objects.filter(company=company_row, account_type="Stock", is_group=False, disabled=False).order_by("lft").first()
            if inv_acc:
                company_row.default_inventory_account = inv_acc
                changes.append("default_inventory_account")
        if not company_row.stock_adjustment_account_id:
            adj_acc = Account.objects.filter(
                company=company_row, account_type="Stock Adjustment",
                account_currency_id=company_row.default_currency_id,
                is_group=False, disabled=False
            ).order_by("lft").first()
            if adj_acc:
                company_row.stock_adjustment_account = adj_acc
                changes.append("stock_adjustment_account")
    if changes:
        company_row.save(update_fields=changes)
    if hasattr(company, "refresh_from_db"):
        company.refresh_from_db()
    return len(plan)



def install_chart_for_company(company, *, chart_template=None, existing_company=None, source=None):
    """High-level service to resolve, plan, and install chart of accounts for a company."""
    if not chart_template and not existing_company and not source:
        if company.create_chart_of_accounts_based_on == "Existing Company" and company.existing_company_id:
            existing_company = company.existing_company
        elif company.chart_of_accounts:
            chart_template = company.chart_of_accounts
        elif company.country_id:
            charts = get_charts_for_country(company.country, with_standard=False)
            chart_template = charts[0] if len(charts) == 1 else "Standard"
        else:
            chart_template = "Standard"

    tree = load_chart(template=chart_template or "Standard", source=source, existing_company=existing_company)
    if not tree:
        return 0
    plan = plan_chart(tree, company)
    return install_chart(company, plan)

