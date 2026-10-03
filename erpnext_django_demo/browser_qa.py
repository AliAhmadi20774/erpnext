"""Capture local demo pages in a real browser using the isolated QA database."""
import argparse
import os
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings_verify")
import django
django.setup()

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--path", required=True)
parser.add_argument("--name", required=True)
parser.add_argument("--width", type=int, default=1440)
args = parser.parse_args()
if args.path in ("partial-sale", "partial-work", "partial-invoice"):
    from demo.models import Order
    sample = Order.objects.get(notes="DEMO-PARTIAL-SALES")
    args.path = (f"/orders/{sample.pk}/detail/" if args.path == "partial-sale" else
                 f"/invoices/{sample.invoice.pk}/print/" if args.path == "partial-invoice" else
                 f"/manufacturing/work-orders/{sample.lines.get().production_plans.get().work_orders.get().pk}/")
if args.path in ("journey", "plan", "scenario", "cost"):
    from demo.models import Order, ProductionPlan
    order = Order.objects.get(notes="DEMO-CUSTOMER-JOURNEY")
    plan = ProductionPlan.objects.filter(source_order_line__order=order).first()
    args.path = (f"/orders/{order.pk}/detail/" if args.path == "journey" else
                 f"/orders/{order.pk}/cost/" if args.path == "cost" else
                 f"/manufacturing/scenarios/{plan.scenarios.first().pk}/" if args.path == "scenario" else
                 f"/manufacturing/plans/{plan.pk}/")
user = get_user_model().objects.get(username="manager")
session = SessionStore()
session["_auth_user_id"] = str(user.pk)
session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
session["_auth_user_hash"] = user.get_session_auth_hash()
session.save()
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="msedge", headless=True)
    context = browser.new_context(viewport={"width": args.width, "height": 1000})
    context.add_cookies([{"name": "sessionid", "value": session.session_key,
                          "url": "http://127.0.0.1:8017/"}])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    response = page.goto("http://127.0.0.1:8017" + args.path)
    page.wait_for_load_state("networkidle")
    assert response.status == 200, response.status
    assert not errors, errors
    assert page.locator("body").evaluate("e => e.scrollWidth <= window.innerWidth"), "Page overflows"
    target = Path("docs") / (args.name + ".png")
    page.screenshot(path=str(target), full_page=True)
    print(f"Browser OK: {args.path}; {args.width}px; {target}")
    browser.close()
session.delete()
