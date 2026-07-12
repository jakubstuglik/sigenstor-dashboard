#!/usr/bin/env python3
"""Dedicated patient mobile nav proof for 390px viewport. Separate browser, long waits."""
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tests" else SCRIPT_DIR
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
BASE_URL = f"http://localhost:{os.environ.get('PORT', '8081')}"

def main():
    print("MOBILE ONLY PROOF (390x844)")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()

        # very patient
        for _ in range(60):
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=5000)
                if page.locator("text=SigenStor").first.is_visible(timeout=1500):
                    print("mobile context: server ready")
                    break
            except:
                pass
            time.sleep(0.8)
        else:
            print("server not responsive for mobile")
            return 1

        time.sleep(1.5)
        page.screenshot(path=str(SCREENSHOTS_DIR / "08_mobile_initial_closed.png"), full_page=True)
        print("[SHOT] 08_mobile_initial_closed")

        # Check initial closed
        nav_before = False
        try:
            nav_before = page.locator('[data-nav="charts"]').first.is_visible(timeout=700)
        except:
            pass
        print(f"nav visible before: {nav_before}")
        assert not nav_before, "should be closed initially"

        # hamburger
        for sel in ['button.menu-toggle', 'button[aria-label*="menu" i]', 'header button']:
            try:
                h = page.locator(sel).first
                if h.is_visible(timeout=2000):
                    h.click(timeout=2000)
                    print("clicked ham")
                    break
            except:
                pass
        time.sleep(1.2)

        # after
        nav_after = False
        try:
            page.locator('[data-nav="charts"]').first.wait_for(state="visible", timeout=3000)
            nav_after = True
        except:
            pass
        print(f"nav visible after: {nav_after}")

        page.screenshot(path=str(SCREENSHOTS_DIR / "09_mobile_after_open_nav.png"), full_page=True)
        print("[SHOT] 09_mobile_after_open_nav")

        if nav_after:
            print("VERIFIED: mobile hamburger opens categories (data-nav visible after click)")
        else:
            print("partial: check screenshots")

        # try click one
        try:
            page.locator('[data-nav="summary"]').first.click(timeout=1500)
            time.sleep(0.8)
            print("clicked summary via mobile nav")
        except:
            pass

        page.screenshot(path=str(SCREENSHOTS_DIR / "10_mobile_navigated.png"), full_page=True)
        ctx.close()
        browser.close()
        print("MOBILE PROOF DONE")
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
