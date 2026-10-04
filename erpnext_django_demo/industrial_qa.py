"""Read-only inspection of the industrial presentation in Chrome."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


base = "http://127.0.0.1:8000"
docs = Path(__file__).resolve().parent / "docs"
errors, results = [], []
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        page.goto(base + "/login/")
        page.locator("#id_username").fill("manager")
        page.locator("#id_password").fill(os.environ.get("ERP_DEMO_PASSWORD", "Demo-1405!"))
        page.get_by_role("button", name="ورود", exact=True).click()
        for width, height in ((1440,900), (390,844)):
            page.set_viewport_size({"width":width,"height":height})
            page.goto(base + "/")
            expect(page.locator(".industrial-presentation")).to_be_visible()
            assert page.locator(".industrial-presentation .stat-card").count() == 3
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
            assert not page.locator(".legacy-presentation").get_attribute("open")
            page.screenshot(path=str(docs / f"industrial-dashboard-{width}.png"))
            main_order = page.locator(".industrial-presentation .stat-card").first.get_attribute("href")
            page.goto(base + main_order)
            expect(page.locator(".details")).to_contain_text("پروژهٔ توسعهٔ ظرفیت")
            expect(page.locator(".details")).to_contain_text("صنایع غذایی سپهر")
            expect(page.locator(".table-panel").first).to_contain_text("PX-2400")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
            config = json.loads(page.locator("#demo-guide-data").text_content())
            plan_url = next(step["url"] for step in config["journey"] if step["title"] == "تقاضا و موعد مبنا")
            tree_url = next(step["url"] for step in config["journey"] if step["title"] == "محصول و تعداد را تعیین کنید")
            if width == 1440:
                page.screenshot(path=str(docs / "industrial-order.png"), full_page=True)
            page.goto(base + tree_url)
            expect(page.locator("#tree-product option:checked")).to_contain_text("PX-2400")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
            page.locator("[data-tree-collapse]").click()
            page.screenshot(path=str(docs / f"industrial-tree-{width}.png"), full_page=True)
            page.goto(base + plan_url)
            expect(page.locator(".table-scroll").first).to_contain_text("PLC صنعتی ۲۴ ورودی و ۱۶ خروجی")
            expect(page.locator(".table-scroll").first).to_contain_text("PX-S07-M1-P1")
            assert "?????" not in page.locator(".page-heading").inner_text()
            if width == 1440:
                page.screenshot(path=str(docs / "industrial-plan.png"))
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
            results.append({"viewport":[width,height],"linked_product_order_and_plan":True,"industrial_cards":3,"horizontal_overflow":False})
        assert not errors, errors
    finally:
        if page.locator(".logout-link").count():
            page.locator(".logout-link").click()
        browser.close()
report={"browser":"Google Chrome","cases":results,"javascript_errors":errors}
(docs / "industrial-browser-verification.json").write_bytes((json.dumps(report,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
print("Industrial presentation passed Chrome desktop and mobile inspection.")
