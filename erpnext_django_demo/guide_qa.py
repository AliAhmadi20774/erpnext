"""Exercise the read-only interactive tour using the running local demo."""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


base = "http://127.0.0.1:8000"
docs = Path(__file__).resolve().parent / "docs"
results, errors, mutations = [], [], []
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width":1440, "height":900})
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("request", lambda request: mutations.append(request.url) if request.method != "GET" and not request.url.endswith(("/login/", "/logout/")) else None)
    try:
        page.goto(base + "/login/")
        page.locator("#id_username").fill("manager")
        page.locator("#id_password").fill(os.environ.get("ERP_DEMO_PASSWORD", "Demo-1405!"))
        page.get_by_role("button", name="ورود", exact=True).click()
        expect(page.locator(".guide-launch")).to_be_visible()
        page.locator(".guide-launch").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#demo-guide")).to_be_visible()
        expect(page.locator("#guide-title")).to_have_text("از اینجا شروع کنید")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.closest('#demo-guide') !== null")
        page.keyboard.press("Escape")
        expect(page.locator("#demo-guide")).not_to_be_visible()
        expect(page.locator(".guide-launch")).to_be_focused()
        page.locator("[data-guide-start=journey]").click()
        expect(page.locator("#demo-guide")).to_be_visible()
        config = json.loads(page.locator("#demo-guide-data").text_content())
        journey = config["journey"]
        for index, entry in enumerate(journey):
            expect(page.locator("#guide-title")).to_have_text(entry["title"])
            assert page.url == base + entry["url"], (index, page.url, entry)
            box = page.locator(".guide-card").bounding_box()
            assert 0 <= box["x"] and box["x"]+box["width"] <= 1441, box
            assert 0 <= box["y"] and box["y"]+box["height"] <= 901, box
            if entry["title"] == "ساختار را در یک نگاه بخوانید":
                page.screenshot(path=str(docs / "guide-desktop.png"))
            page.locator("#guide-next").click()
        expect(page.locator("#demo-guide")).not_to_be_visible()
        results.append({"presentation_steps":len(journey), "page_navigation":True, "keyboard_focus":True})
        page.goto(base + "/workspace/")
        page.locator("[data-guide-start=journey]").click()
        page.locator("#guide-next").click()
        page.keyboard.press("Escape")
        page.reload()
        page.locator("[data-guide-start=journey]").click()
        expect(page.locator("#guide-title")).to_have_text("فرایند را انتخاب کنید")
        page.locator("#guide-reset").click()
        expect(page.locator("#guide-title")).to_have_text("از اینجا شروع کنید")
        page.keyboard.press("Escape")
        results.append({"resume_after_reload":True,"restart":True})
        for width, height in ((390,844), (390,500), (1024,768)):
            page.set_viewport_size({"width":width, "height":height})
            page.goto(base + "/products/tree/")
            page.locator(".guide-launch").click()
            for index in range(3):
                expect(page.locator("#demo-guide")).to_be_visible()
                box = page.locator(".guide-card").bounding_box()
                assert box["x"] >= 0 and box["x"]+box["width"] <= width+1, box
                assert box["y"] >= 0 and box["y"]+box["height"] <= height+1, box
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                if width == 390 and height == 844 and index == 1:
                    page.screenshot(path=str(docs / "guide-mobile.png"))
                page.locator("#guide-next").click()
            expect(page.locator("#demo-guide")).not_to_be_visible()
            results.append({"viewport":[width,height], "card_within_viewport":True})
        # Storage writes and blocked storage getters must not break the tour.
        page.goto(base + "/workspace/")
        page.evaluate("() => {Storage.prototype.setItem = () => {throw new Error('disabled storage');};}")
        page.locator("[data-guide-start=journey]").click()
        expect(page.locator("#demo-guide")).to_be_visible()
        page.keyboard.press("Escape")
        page.evaluate("() => {Object.defineProperty(window,'localStorage',{get(){throw new Error('blocked storage');}});}")
        page.locator(".guide-launch").click()
        expect(page.locator("#demo-guide")).to_be_visible()
        page.keyboard.press("Escape")
        results.append({"storage_quota_failure":True,"blocked_storage_access":True})
        assert not errors, errors
        assert not mutations, mutations
    finally:
        if page.locator(".logout-link").count():
            page.locator(".logout-link").click()
        browser.close()
report = {"browser":"Google Chrome", "results":results, "javascript_errors":errors, "business_posts":mutations}
(docs / "guide-verification.json").write_bytes((json.dumps(report, ensure_ascii=False, indent=2)+"\n").encode("utf-8"))
print(f"Interactive guide passed: {len(journey)} presentation steps and 3 responsive cases; no business mutations.")
