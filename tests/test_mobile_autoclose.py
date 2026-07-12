#!/usr/bin/env python3
"""
Reusable test for mobile hamburger menu auto-close behavior.

When the drawer is opened via hamburger on narrow viewport and a nav item
is clicked, the drawer should automatically close (fold).

Run with:
    $env:PORT=8081; $env:SIGENSTOR_DB='data/sigenstor_dev.db'; python tests/test_mobile_autoclose.py
"""
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE_URL = f"http://localhost:{os.environ.get('PORT', '8081')}"
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tests" else SCRIPT_DIR
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


def test_mobile_menu_closes_on_nav_click():
    """Verify that tapping a nav item in the mobile menu closes the drawer."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        page = context.new_page()

        # Wait for server
        for _ in range(30):
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=5000)
                if page.locator("text=SigenStor").first.is_visible(timeout=1500):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        # Open hamburger
        ham = page.locator('button.menu-toggle, button[aria-label*="menu" i]').first
        ham.wait_for(state="visible", timeout=5000)
        ham.click(timeout=2000)
        time.sleep(0.6)

        # Should be open
        nav_visible = page.locator('[data-nav="charts"]').first.is_visible(timeout=1500)
        assert nav_visible, "Menu should be open after clicking hamburger"

        # Click a nav item
        page.locator('[data-nav="charts"]').first.click(timeout=2000)
        time.sleep(0.8)

        # Should now be closed
        nav_still_visible = False
        try:
            nav_still_visible = page.locator('[data-nav="charts"]').first.is_visible(timeout=600)
        except Exception:
            nav_still_visible = False

        assert not nav_still_visible, "Menu should close automatically after selecting a category"

        # Take evidence screenshot
        shot = SCREENSHOTS_DIR / "mobile_autoclose_after_nav.png"
        page.screenshot(path=str(shot), full_page=True)
        print(f"[SHOT] {shot}")

        context.close()
        browser.close()

        print("PASS: Mobile menu auto-closes after nav item click")


if __name__ == "__main__":
    test_mobile_menu_closes_on_nav_click()
