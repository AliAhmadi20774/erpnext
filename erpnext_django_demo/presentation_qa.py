"""Execute the customer presentation through Edge against the isolated QA database.

Requires runserver with config.settings_verify on port 8017 and a fresh reset of that
database. All business mutations use the same forms and CSRF checks as a presenter.
"""
import json
import os
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_verify")
# Playwright sync runs its own event loop. This single-threaded QA script performs
# ORM reads between browser actions; it does not run concurrent Django handlers.
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
import django
django.setup()

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.db.models import Sum
from django.utils import timezone
from playwright.sync_api import sync_playwright

from demo.approvals import approval_needed
from demo.exceptions import build_exception_alerts
from demo.models import Item, JournalEntry, Order, Supplier, WorkOrder

assert Path(settings.DATABASES["default"]["NAME"]).name == "manager-verify.sqlite3", "QA database only"
order = Order.objects.get(notes="DEMO-CUSTOMER-JOURNEY")
plan = order.lines.get().production_plans.get()
assert not order.fulfillment_batches.exists(), "Reset isolated QA data before this walkthrough"
baseline = {item.sku: item.stock for item in Item.objects.all()}
initial_alerts = [row for row in build_exception_alerts() if row["affected"] == order.number]
manager = get_user_model().objects.get(username="manager")
session = SessionStore()
session.update({"_auth_user_id": str(manager.pk),
                "_auth_user_backend": "django.contrib.auth.backends.ModelBackend",
                "_auth_user_hash": manager.get_session_auth_hash()})
session.save()
actions = []

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{"name": "sessionid", "value": session.session_key,
                              "url": "http://127.0.0.1:8017/"}])
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))

        def visit(path):
            response = page.goto("http://127.0.0.1:8017" + path)
            assert response.status == 200, (path, response.status)

        def click(selector):
            page.locator(selector).click()
            page.wait_for_load_state("networkidle")
            assert not page.locator(".alert.error, .error-text, .field-error").count(), page.locator("body").inner_text()

        def action(path):
            click(f'form[action="{path}"] button[type="submit"]')
            actions.append(path)

        visit("/")
        page.screenshot(path="docs/presentation-dashboard.png", full_page=True)
        visit(f"/orders/{order.pk}/detail/")
        page.screenshot(path="docs/presentation-before.png", full_page=True)
        supplier = Supplier.objects.get(code="SUP-001")
        for line in plan.lines.filter(supply_type="buy", net_requirement__gt=0):
            purchases = list(line.purchase_orders.exclude(status="cancelled"))
            if not purchases:
                visit(f"/orders/purchase/new/?plan={plan.pk}&line={line.pk}&item={line.item_id}")
                page.locator("#id_party").select_option(str(supplier.pk))
                page.locator("#id_due_date").fill(timezone.localdate().isoformat())
                page.locator("#id_notes").fill("QA-PRESENTATION-SUPPLY")
                click('form.order-layout button[type="submit"]')
                purchases = list(line.purchase_orders.exclude(status="cancelled"))
                assert len(purchases) == 1
            for purchase in purchases:
                visit(f"/orders/{purchase.pk}/detail/")
                if approval_needed(purchase):
                    if purchase.approval_status != "pending":
                        page.locator('form[action$="/request/"] textarea').fill("تامین مواد سفارش ۴۰ دستگاهی")
                        click('form[action$="/request/"] button')
                    visit("/purchasing/approvals/")
                    form = page.locator(f'form[action="/purchasing/approvals/{purchase.pk}/decide/"]')
                    form.locator("textarea").fill("تایید تامین برای رفع توقف سفارش مشتری")
                    click(f'form[action="/purchasing/approvals/{purchase.pk}/decide/"] button[value="approve"]')
                    actions.append(f"approve:{purchase.pk}")
                    visit(f"/orders/{purchase.pk}/detail/")
                action(f"/orders/{purchase.pk}/confirm/")
                action(f"/orders/{purchase.pk}/fulfill/")
                action(f"/orders/{purchase.pk}/invoice/")

        for line in plan.lines.filter(supply_type="make", net_requirement__gt=0).order_by("-level"):
            works = list(line.work_orders.exclude(status="cancelled"))
            if not works:
                visit(f"/manufacturing/work-orders/new/?bom={line.supply_bom_id}&quantity={line.net_requirement}"
                      f"&plan={plan.pk}&line={line.pk}")
                page.locator("#id_notes").fill("QA-PRESENTATION-PRODUCTION")
                click('form button.button[type="submit"]')
                works = list(line.work_orders.exclude(status="cancelled"))
                assert len(works) == 1
            for work in works:
                visit(f"/manufacturing/work-orders/{work.pk}/")
                if work.status == WorkOrder.DRAFT:
                    action(f"/manufacturing/work-orders/{work.pk}/release/")
                action(f"/manufacturing/work-orders/{work.pk}/complete/")

        visit(f"/orders/{order.pk}/detail/")
        action(f"/orders/{order.pk}/fulfill/")
        action(f"/orders/{order.pk}/invoice/")
        page.locator(f'a[href="/orders/{order.pk}/payment/"]').click()
        page.locator("#id_reference").fill("QA-PRESENTATION-PAID")
        click('form button.button[type="submit"]')
        page.screenshot(path="docs/presentation-after.png", full_page=True)
        order.refresh_from_db()
        assert order.workflow_label == "تسویه شده"
        assert order.invoice.amount == order.total and order.invoice.balance == 0
        assert order.lines.get().remaining_quantity == 0
        assert not [row for row in build_exception_alerts() if row["affected"] == order.number]
        assert not errors, errors
        browser.close()

    for item in Item.objects.all():
        balance = 0
        for movement in item.movements.order_by("created_at", "pk"):
            assert movement.balance_before == balance
            balance += movement.change
            assert movement.balance_after == balance
        assert item.stock == balance
    for entry in JournalEntry.objects.all():
        totals = entry.lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        assert totals["debit"] == totals["credit"]
    evidence = {"date": timezone.localdate().isoformat(), "order": order.number,
                "quantity": 40, "amount": str(order.total), "balance": "0",
                "baseline_stock": baseline, "final_stock": dict(Item.objects.values_list("sku", "stock")),
                "initial_alerts": len(initial_alerts), "final_affected_alerts": 0,
                "actions": actions, "journal_count": JournalEntry.objects.count(),
                "inventory_chain_verified": True, "journals_balanced": True}
    Path("docs/presentation-verification.json").write_bytes(
        json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf8"))
    print("Presentation browser walkthrough passed: customer 40 -> supply -> produce -> fulfill -> paid")
finally:
    session.delete()
