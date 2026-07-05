#!/usr/bin/env python3
"""Playwright test for summary page loading and hints."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

# Resolve screenshots relative to project root (works when run from root or from tests/)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tests" else SCRIPT_DIR
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
BASE_URL = "http://localhost:8080"

def main():
    print("=== Playwright Summary Page Test ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        # Go to app
        page.goto(BASE_URL, timeout=10000)
        time.sleep(2)

        # Click SUMMARY in sidebar
        try:
            page.locator("text=SUMMARY").first.click()
        except:
            page.locator("a[href*='summary'], button:has-text('SUMMARY')").first.click()
        time.sleep(3)  # wait for load_all to run

        # Take screenshot of the page
        screenshot_path = SCREENSHOTS_DIR / "summary_page_test.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"[SHOT] {screenshot_path}")

        # Check if summary cards are visible
        try:
            cards = page.locator("text=Today, text=Yesterday, text=This Week, text=This Month").all()
            print(f"Found {len(cards)} period labels")
        except Exception as e:
            print(f"Error finding periods: {e}")

        # Check for Self value with 2 decimals
        try:
            self_labels = page.locator("text=Self:").all_text_contents()
            print(f"Self labels: {self_labels}")
        except Exception as e:
            print(f"Error finding Self: {e}")

        # Check for ? hints (the divs with ? )
        try:
            hints = page.locator("text=?").all()
            print(f"Found {len(hints)} ? elements")
        except Exception as e:
            print(f"Error finding ? : {e}")

        # Try to hover a ? to see tooltip
        try:
            first_q = page.locator("text=?").first
            first_q.hover()
            time.sleep(1)
            tooltip = page.locator(".q-tooltip, [role=tooltip], .tooltip").first
            if tooltip.is_visible(timeout=2000):
                tooltip_text = tooltip.inner_text()
                print(f"Tooltip text: {tooltip_text}")
            else:
                print("No tooltip visible after hover")
        except Exception as e:
            print(f"Error hovering for tooltip: {e}")

        browser.close()
        print("Test completed. Check the screenshot.")

if __name__ == "__main__":
    main()
