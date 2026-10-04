"""Verify navigation containment and scrolling against the running local demo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import sync_playwright


parser = argparse.ArgumentParser()
parser.add_argument("--base-url", default="http://127.0.0.1:8000")
parser.add_argument("--browser", choices=("msedge", "chrome"), default="msedge")
args = parser.parse_args()
base = args.base_url.rstrip("/")
parsed = urlparse(base)
assert parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1"), "Local QA only"
sizes = [(1920, 920), (1440, 900), (1024, 768), (1440, 480), (751, 600), (750, 600), (390, 844), (390, 500)]
results = []
suffix = "-chrome" if args.browser == "chrome" else ""

with sync_playwright() as pw:
    browser = pw.chromium.launch(channel=args.browser, headless=True)
    try:
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        assert page.goto(base + "/login/").status == 200
        page.locator("#id_username").fill("manager")
        page.locator("#id_password").fill(os.environ.get("ERP_DEMO_PASSWORD", "Demo-1405!"))
        page.get_by_role("button", name="ورود", exact=True).click()
        page.locator(".logout-link").wait_for(state="visible")
        for width, height in sizes:
            page.set_viewport_size({"width": width, "height": height})
            assert page.goto(base + "/audit/").status == 200
            page.wait_for_load_state("networkidle")
            for selector in ("link[rel=stylesheet]", "script[src*='demo/ui.js']"):
                node = page.locator(selector)
                resource = node.get_attribute("href" if selector.startswith("link") else "src")
                version = parse_qs(urlparse(resource).query).get("v")
                assert version, "Page still uses an unversioned asset URL"
                response = page.request.get(urljoin(base, resource))
                assert response.status == 200
                assert version == [hashlib.sha256(response.body()).hexdigest()[:12]], "Asset version does not match served content"
            mobile = width <= 750
            if mobile:
                page.locator(".mobile-menu").click()
                page.wait_for_function("getComputedStyle(document.querySelector('.sidebar')).right === '0px'")
            sidebar = page.locator(".sidebar")
            nav = sidebar.locator(".nav")
            measurements = sidebar.evaluate("""side => {
                const rect = side.getBoundingClientRect();
                const nav = side.querySelector('.nav');
                const foot = side.querySelector('.sidebar-bottom').getBoundingClientRect();
                const brand = side.querySelector('.brand').getBoundingClientRect();
                return {top: rect.top, bottom: rect.bottom, height: rect.height,
                    footerBottom: foot.bottom, brandTop: brand.top,
                    navHeight: nav.clientHeight, navContent: nav.scrollHeight};
            }""")
            assert abs(measurements["height"] - height) <= 1, measurements
            assert measurements["brandTop"] >= measurements["top"], measurements
            assert measurements["footerBottom"] <= measurements["bottom"], measurements
            assert measurements["navContent"] > measurements["navHeight"] > 0, measurements
            assert nav.locator("a.active").evaluate("""link => {
                const box = link.getBoundingClientRect();
                const nav = link.closest('.nav').getBoundingClientRect();
                return box.top >= nav.top - 1 && box.bottom <= nav.bottom + 1;
            }"""), "Current page is hidden in the navigation"
            # Focus must reveal the last link inside the navigation viewport,
            # rather than pushing the page or exposing links below its background.
            last = nav.locator("a").last
            last.focus()
            assert last.evaluate("""link => {
                const box = link.getBoundingClientRect();
                const nav = link.closest('.nav').getBoundingClientRect();
                return box.top >= nav.top - 1 && box.bottom <= nav.bottom + 1;
            }"""), "Last navigation link cannot be reached"
            assert nav.evaluate("node => node.scrollTop") > 0
            # Scrolling past the menu's bottom must not scroll the main page.
            page_scroll = page.evaluate("window.scrollY")
            nav.evaluate("node => { node.scrollTop = node.scrollHeight; }")
            nav.hover()
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(100)
            assert page.evaluate("window.scrollY") == page_scroll, "Menu scroll leaks into the page"
            if mobile:
                assert page.evaluate("getComputedStyle(document.body).overflowY") == "hidden"
            else:
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                page.wait_for_timeout(100)
                assert abs(sidebar.bounding_box()["y"]) <= 1, "Sidebar does not stay in the viewport"
            assert page.locator("body").evaluate("node => node.scrollWidth <= innerWidth"), "Page overflows"
            assert not errors, errors
            if (width, height) in ((1920, 920), (1440, 480), (390, 500)):
                page.screenshot(path=str(Path("docs") / f"sidebar{suffix}-{width}x{height}.png"), full_page=False)
            if mobile:
                page.keyboard.press("Escape")
                assert page.locator(".mobile-menu").get_attribute("aria-expanded") == "false"
                assert not page.evaluate("document.body.classList.contains('menu-open')")
                assert page.evaluate("getComputedStyle(document.body).overflowY") != "hidden"
            results.append({"width": width, "height": height, "mobile": mobile,
                            "navigation_contained": True, "last_link_reachable": True,
                            "active_link_revealed": True, "scroll_contained": True})
            print(f"Sidebar OK: {width}x{height}")
        page.locator(".logout-link").click()
    finally:
        browser.close()

Path(f"docs/sidebar{suffix}-verification.json").write_bytes((json.dumps({
    "browser": "Google Chrome" if args.browser == "chrome" else "Microsoft Edge",
    "page": "/audit/", "cases": results, "served_asset_versions_verified": True,
    "javascript_errors": errors, "horizontal_page_overflow": False,
}, indent=2) + "\n").encode("utf-8"))
