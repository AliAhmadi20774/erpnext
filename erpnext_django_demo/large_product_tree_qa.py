"""Read-only Chrome checks of the large BOM against a running local demo."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
args = parser.parse_args()
base = args.base_url.rstrip("/")
parsed = urlparse(base)
assert parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"), "Local QA only"
results = []
docs = Path(__file__).resolve().parent / "docs"

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True)
    try:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        assert page.goto(base + "/login/").status == 200
        page.locator("#id_username").fill("manager")
        page.locator("#id_password").fill(os.environ.get("ERP_DEMO_PASSWORD", "Demo-1405!"))
        page.get_by_role("button", name="ورود", exact=True).click()
        page.locator(".logout-link").wait_for(state="visible")
        for width, height in ((1920, 920), (1440, 900), (1024, 768), (390, 844)):
            page.set_viewport_size({"width": width, "height": height})
            assert page.goto(base + "/products/tree/").status == 200
            page.wait_for_load_state("networkidle")
            original = page.locator("#tree-product option").filter(has_text="LINE-500")
            page.select_option("#tree-product", original.get_attribute("value"))
            page.locator(".product-selector button").click()
            page.locator(".page-heading h1").wait_for()
            assert page.locator("#tree-product option:checked").inner_text().endswith("LINE-500")
            assert page.locator(".product-node").count() == 175
            assert page.locator(".level-4").count() == 56
            assert page.locator(".product-tree > li > details > ul > li").count() == 8
            assert page.locator(".product-node > details > summary:visible").count() == 33
            page.get_by_role("button", name="جمع کردن شاخه‌ها", exact=True).click()
            assert page.locator(".product-node > details > summary:visible").count() == 9
            page.get_by_role("button", name="باز کردن همه", exact=True).click()
            assert page.locator(".product-node > details > summary:visible").count() == 175
            # Deep rows remain inside their own scroll area, including on mobile.
            geometry = page.locator(".product-tree-scroll").evaluate("""node => ({
                viewport: node.clientWidth, content: node.scrollWidth,
                page: document.documentElement.scrollWidth, window: innerWidth
            })""")
            assert geometry["page"] <= geometry["window"] + 1, geometry
            assert not errors, errors
            page.get_by_role("button", name="جمع کردن شاخه‌ها", exact=True).click()
            page.evaluate("scrollTo(0, 0)")
            if width in (1440, 390):
                page.screenshot(path=str(docs / f"large-product-tree-{width}.png"), full_page=True)
            # Open one mechanical branch all the way down to the common drive.
            branch = page.locator(".product-tree > li > details > ul > li").first
            branch.locator(":scope > details > summary").click()
            module = branch.locator(":scope > details > ul > li").nth(1)
            module.locator(":scope > details > summary").click()
            drive = module.locator(":scope > details > ul > li").last
            drive.locator(":scope > details > summary").click()
            assert drive.locator(".level-4 > details > summary:visible").count() == 4
            if width == 1440:
                page.locator(".product-tree-panel").scroll_into_view_if_needed()
                page.screenshot(path=str(docs / "large-product-tree-deep.png"))
            # Use real GET forms to select a ten-unit BOM, recalculate, then return.
            option = page.locator("#tree-product option").filter(has_text="LP-S07-M2")
            page.select_option("#tree-product", option.get_attribute("value"))
            page.locator(".product-selector button").click()
            page.locator("#tree-quantity").fill("3")
            page.locator(".production-plan button").click()
            assert page.locator("#tree-product option:checked").inner_text().endswith("LP-S07-M2")
            assert page.locator("#tree-quantity").input_value() == "3"
            required = page.locator(".level-1").first.locator(".tree-measure strong").inner_text()
            assert required.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٫", "0123456789.")).startswith("7.65"), required
            results.append({"viewport": [width, height], "nodes": 175,
                            "deepest_level": 5, "expand_collapse": True,
                            "selector_and_batch_quantity": True, "horizontal_page_overflow": False})
    finally:
        if page.locator(".logout-link").count():
            page.locator(".logout-link").click()
        browser.close()

report = {"browser": "Google Chrome", "cases": results, "javascript_errors": errors}
(docs / "large-product-tree-browser-verification.json").write_bytes(
    (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
print(f"Large product tree passed {len(results)} Chrome viewport checks.")
