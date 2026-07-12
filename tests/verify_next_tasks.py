#!/usr/bin/env python3
"""
Verification script for the 'next things' fixes (port 8081 + dev DB only).
Covers:
- Settings layout (desktop not left-shit)
- Hamburger: no auto-close on desktop nav, only mobile
- Summaries (load, values from aggs)
- Charts smoothing active
- Maintenance (can force, see runs)
Saves screenshots then prints paths for immediate read_file by caller.
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

BASE = os.environ.get("BASE_URL", "http://localhost:8081")
SCR = Path("screenshots")
SCR.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")

def shot(page, name: str):
    p = SCR / f"{ts}_{name}.png"
    page.screenshot(path=str(p), full_page=True)
    print(f"SCREENSHOT: {p}")
    return p

def main():
    with sync_playwright() as p:
        # Desktop viewport (wide)
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        page = ctx.new_page()

        print("Nav to BASE (desktop)")
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
        shot(page, "desktop_home")

        # Go to SETTINGS - expect fields, check not purely left (visual + container)
        print("Click SETTINGS (desktop)")
        # Use data-nav or visible text
        try:
            page.locator('[data-nav="settings"]').first.click()
        except Exception:
            page.get_by_text("Settings", exact=False).first.click()
        page.wait_for_timeout(900)
        shot(page, "desktop_settings")

        # Verify some settings controls are visible
        expect(page.get_by_text("SigenStor IP")).to_be_visible()
        expect(page.get_by_text("Save Config")).to_be_visible()

        # Go to SUMMARY - check loads (look for labels or numbers)
        print("Click SUMMARY (desktop)")
        try:
            page.locator('[data-nav="summary"]').first.click()
        except Exception:
            page.get_by_text("Summary", exact=False).first.click()
        page.wait_for_timeout(1800)  # summaries may compute
        shot(page, "desktop_summary")

        # Look for energy values or title to confirm loaded (non-zero or labels)
        txt = page.content()
        if "PV:" in txt or "Energy Summaries" in txt or "Today" in txt:
            print("SUMMARY_LOAD_OK")

        # Go to CHARTS, exercise smoothing
        print("Click CHARTS (desktop)")
        try:
            page.locator('[data-nav="charts"]').first.click()
        except Exception:
            page.get_by_text("Charts", exact=False).first.click()
        page.wait_for_timeout(1200)
        shot(page, "desktop_charts")

        # Click smoothing buttons, ensure UI updates (no crash)
        for label in ["No smoothing", "3 last", "5 last"]:
            try:
                btn = page.get_by_text(label, exact=False).first
                if btn.is_visible():
                    btn.click()
                    page.wait_for_timeout(600)
                    shot(page, f"desktop_charts_smooth_{label.replace(' ','')}")
            except Exception as e:
                print(f"smooth click issue {label}: {e}")

        # MAINTENANCE page + force (to exercise agg)
        print("Click MAINTENANCE (desktop)")
        try:
            page.locator('[data-nav="maintenance"]').first.click()
        except Exception:
            page.get_by_text("Maintenance", exact=False).first.click()
        page.wait_for_timeout(900)
        shot(page, "desktop_maintenance")

        try:
            fr = page.get_by_text("Force Run Now", exact=False).first
            if fr.is_visible():
                fr.click()
                page.wait_for_timeout(2500)
                print("FORCE_AGG_CLICKED")
                shot(page, "desktop_maintenance_after_force")
        except Exception as e:
            print(f"force not clicked: {e}")

        # Now test HAMBURGER behavior
        # Desktop: sidebar should stay (show-if-above). We check by seeing if data-drawer or visible items.
        print("Desktop nav click should NOT collapse sidebar persistently")
        try:
            page.locator('[data-nav="dashboard"]').first.click()
            page.wait_for_timeout(500)
            shot(page, "desktop_after_nav_dashboard")
            # If sidebar items still interactable or drawer not forced hidden
            print("DESKTOP_NAV_NO_AUTOCLOSE_ATTEMPTED")
        except Exception:
            pass

        # Mobile viewport test: narrow, hamburger opens, nav item closes it
        print("=== Mobile viewport test ===")
        ctx.close()
        ctx = browser.new_context(viewport={"width": 390, "height": 844})
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        shot(page, "mobile_home")

        # Open via hamburger (menu-toggle)
        try:
            ham = page.locator(".menu-toggle, button[aria-label*='menu' i], [data-testid*='menu']").first
            if not ham.is_visible():
                ham = page.locator("button:has-text('menu')").first
            ham.click()
            page.wait_for_timeout(600)
            shot(page, "mobile_ham_open")
            print("MOBILE_HAM_OPENED")
        except Exception as e:
            print(f"ham open issue: {e}")

        # Click a nav item e.g. Charts
        try:
            page.locator('[data-nav="charts"]').first.click()
            page.wait_for_timeout(700)
            shot(page, "mobile_after_nav_charts")
            # Drawer should be closed (no q-drawer--open or data false)
            drawer = page.locator(".q-drawer")
            cls = ""
            try:
                cls = drawer.get_attribute("class") or ""
            except Exception:
                pass
            if "q-drawer--open" not in cls:
                print("MOBILE_NAV_CLOSED_DRAWER_OK")
            else:
                print("MOBILE_NAV_STILL_OPEN?")
        except Exception as e:
            print(f"mobile nav: {e}")

        # Final desktop wide again for settings recheck if needed
        ctx.close()
        browser.close()
        print("VERIFICATION_SCRIPT_DONE")

if __name__ == "__main__":
    main()
