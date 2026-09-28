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


def load_chart(*, template="Standard", source=None):
    if source is not None:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        tree = data.get("tree") if isinstance(data, dict) else None
        if not isinstance(tree, dict):
            raise ValueError("Chart JSON must contain a tree object.")
        return tree
    if template not in STANDARD_TEMPLATES:
        raise ValueError(f"Unknown chart template: {template}")
    module = ast.parse((CHART_DIRECTORY / STANDARD_TEMPLATES[template]).read_text(encoding="utf-8"))
    function = next((node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "get"), None)
    if function is None:
        raise ValueError("Standard chart has no get function.")
    result = next((node.value for node in function.body if isinstance(node, ast.Return)), None)
    if result is None:
        raise ValueError("Standard chart has no return value.")
    return ast.literal_eval(_IdentityTranslation().visit(result))


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
    company = Company.objects.select_for_update().get(pk=company.pk)
    expected = {row.name: row for row in plan}
    existing = list(Account.objects.filter(company=company))
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
            company=company, parent_account_id=row.parent_name, is_group=row.is_group,
            root_type=row.root_type, report_type=row.report_type, account_type=row.account_type,
            account_currency_id=row.currency_id,
        )
    changes = []
    for field, account_type in (("default_receivable_account", "Receivable"), ("default_payable_account", "Payable")):
        if not getattr(company, f"{field}_id"):
            account = Account.objects.filter(company=company, account_type=account_type, is_group=False, disabled=False).order_by("lft").first()
            if account:
                setattr(company, field, account)
                changes.append(field)
    if changes:
        company.save(update_fields=changes)
    return len(plan)
