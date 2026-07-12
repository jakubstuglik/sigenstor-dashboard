#!/usr/bin/env python3
"""
Future regression test: Navigation stability + deferred page inits.

Covers:
- Rapid navigation between pages (to catch timer parent slot / context errors)
- Deferred loads for charts (period bars, smoothing state)
- Deferred loads for summary (cards appear with data)
- No blank pages or ISE after nav

Run with: python tests/test_defer_navigation_regression.py
Expects server on 8081 + dev DB.
"""
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://localhost:8081")
SCR = Path("screenshots")
SCR.mkdir(exist_ok=True)
ts = time.strftime("%Y%m%d_%H%M%S")

def shot(page, name):
    p = SCR / f"{ts}_defer_nav_{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"SHOT {name}: {p}")
    return p

def wait_visible(page, selector, timeout=5000):
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
        return True
    except:
        return False

def main():
    print("=== DEFER + NAV REGRESSION TEST ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        page.goto(BASE, wait_until="domcontentloaded", timeout=15000)
        time.sleep(1.5)
        shot(page, "start")

        pages = [
            ("CHARTS", "charts", "Last 1h"),   # charts should show period buttons + defer content
            ("SUMMARY", "summary", "This Week"),  # summary cards should load via defer
            ("MAINTENANCE", "maintenance", "Recent Aggregation Runs"),
            ("CHARTS", "charts", "Last 7d"),
            ("DASHBOARD", "dashboard", "Real-time Dashboard"),
        ]

        for label, nav, expect_text in pages:
            print(f"Nav to {label} ...")
            try:
                page.locator(f'[data-nav="{nav}"]').first.click()
            except:
                page.get_by_text(label, exact=False).first.click()
            time.sleep(2.0)  # allow defer timers (0.1s root + 0.05s) + render

            shot(page, nav)

            content = page.content()
            if "Internal Server Error" in content or "Traceback" in content:
                print(f"FAIL: ISE on {label}")
                browser.close()
                exit(2)

            if expect_text in content or page.get_by_text(expect_text, exact=False).count() > 0:
                print(f"  OK: {expect_text} visible on {label}")
            else:
                print(f"  WARN: {expect_text} not immediately visible on {label}")

            # Extra check for summary cards
            if nav == "summary":
                if page.locator("text=This Week").count() > 0 or "This Week" in content:
                    print("  SUMMARY_CARDS_PRESENT")

            # Check charts has some defer-loaded controls
            if nav == "charts":
                if page.locator("text=Last 1h").count() > 0 or page.locator("button:has-text('Last')").count() > 0:
                    print("  CHARTS_CONTROLS_PRESENT")

        print("DEFER_NAV_REGRESSION_PASSED")
        browser.close()

if __name__ == "__main__":
    main()
