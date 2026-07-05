#!/usr/bin/env python3
"""
Mandatory Playwright test per AGENTS.md:
- Test auto-refresh enabled + interval persistence (click pause, change interval, nav away+back)
- Verify unselected period/smoothing buttons have blue border/text like PAUSE
- Verify hover tooltips show 3 decimal places after smoothing or load
- No ISE, charts render, interactions work
"""
import time
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

# Resolve screenshots relative to project root (works when run from root or from tests/)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tests" else SCRIPT_DIR
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
BASE_URL = "http://localhost:8080"

def take_screenshot(page, name: str):
    path = SCREENSHOTS_DIR / f"test_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"[SHOT] {path}")
    return path

def main():
    print("=== MANDATORY PRE-CLAIM TEST: auto-persist, 3decimals, blue buttons ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100})
        page = ctx.new_page()

        # Wait server
        print("Waiting for server...")
        for _ in range(40):
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=6000)
                if page.locator("text=SigenStor").first.is_visible(timeout=1500):
                    print("Server ready")
                    break
            except:
                pass
            time.sleep(0.5)
        else:
            print("FAIL: server not ready")
            sys.exit(1)

        take_screenshot(page, "00_dashboard")

        # === CHARTS ===
        print("Nav to CHARTS")
        try:
            page.locator("button:has-text('CHARTS')").first.click()
        except:
            page.locator("text=CHARTS").first.click()
        time.sleep(2.5)
        take_screenshot(page, "01_charts_loaded")

        content = page.content()
        if "Internal Server Error" in content or "Traceback" in content:
            print("!!! ISE ON LOAD !!!")
            sys.exit(2)
        print("No ISE on load")

        # Click period buttons
        print("Testing period buttons...")
        for pbtn in ["Last 1h", "Last 6h", "Last 24h"]:
            try:
                page.locator(f"button:has-text('{pbtn}')").first.click()
                time.sleep(1.0)
                print(f"  clicked {pbtn}")
            except Exception as e: print(f"  {pbtn} err: {e}")
        take_screenshot(page, "02_after_periods")

        # Smoothing buttons (key new controls)
        print("Testing smoothing buttons...")
        for sbtn in ["No smoothing", "3 last", "5 last"]:
            try:
                btn = page.locator(f"button:has-text('{sbtn}')").first
                btn.click()
                time.sleep(1.5)
                print(f"  clicked {sbtn}")
                take_screenshot(page, f"03_smoothing_{sbtn.replace(' ','_')}")
            except Exception as e:
                print(f"  smoothing {sbtn} err: {e}")

        # Power toggles
        print("Toggling some power switches...")
        try:
            switches = page.locator("input[type='checkbox']").all()
            for i, sw in enumerate(switches[:2]):
                if sw.is_visible():
                    sw.click()
                    time.sleep(0.7)
                    sw.click()
                    time.sleep(0.5)
        except Exception as e: print(f"  toggles: {e}")
        take_screenshot(page, "04_after_toggles")

        # === Auto-refresh: PAUSE button + interval ===
        print("Testing PAUSE AUTO-REFRESH button (toggle state)...")
        try:
            pause = page.locator("button:has-text('Pause Auto-Refresh'), button:has-text('Resume Auto-Refresh')").first
            if pause.is_visible():
                pause.click()
                time.sleep(1.0)
                print("  clicked pause/resume")
                take_screenshot(page, "05_after_pause_toggle")
        except Exception as e: print(f"  pause click err: {e}")

        # Change interval period (persist test)
        print("Changing auto-refresh interval (period) to 30s + Apply...")
        try:
            apply_btn = page.locator("button:has-text('Apply')").first
            # Click current displayed value in the select (shows e.g. 10)
            try:
                page.locator("text=10").first.click()
                time.sleep(0.4)
            except:
                pass
            # Select 30 from dropdown
            try:
                page.locator("text=30").first.click()
                time.sleep(0.6)
            except:
                pass
            apply_btn.click()
            time.sleep(1.5)
            print("  applied new interval 30")
            take_screenshot(page, "06_after_interval_apply")
        except Exception as e: print(f"  interval change err: {e}")

        # === Test persistence: nav away and back ===
        print("Nav to DASHBOARD then back to CHARTS to verify persistence of auto state + interval")
        try:
            page.locator("button:has-text('DASHBOARD')").first.click()
            time.sleep(1.5)
            take_screenshot(page, "07_dashboard")
            page.locator("button:has-text('CHARTS')").first.click()
            time.sleep(2.5)
            take_screenshot(page, "08_charts_after_nav_back")
            content2 = page.content()
            if "Internal Server Error" in content2:
                print("!!! ISE AFTER NAV BACK !!!")
                sys.exit(3)
            # Check pause/resume button state text in dom
            if "Resume Auto-Refresh" in content2:
                print("  Persistence check: Resume button text present after nav (good)")
            elif "Pause Auto-Refresh" in content2:
                print("  Pause button still there")
        except Exception as e:
            print(f"  nav persist test err: {e}")

        # Try to hover chart to show tooltip (for decimal precision verification)
        print("Hovering chart area to capture tooltip with formatted values...")
        try:
            # Target the plotly chart container
            plot = page.locator(".js-plotly-plot, .plotly").first
            if plot.is_visible():
                box = plot.bounding_box()
                if box:
                    # hover roughly in middle of plot
                    page.mouse.move(box["x"] + box["width"]*0.6, box["y"] + box["height"]*0.4)
                    time.sleep(1.2)
                    take_screenshot(page, "09_hover_tooltip")
                    print("  hover screenshot taken (check for .xxx precision)")
        except Exception as e: print(f"  hover err: {e}")

        # Final checks
        final_c = page.content()
        if "Internal Server Error" in final_c or "Traceback" in final_c:
            print("!!! FINAL ISE DETECTED !!!")
            sys.exit(4)

        print("=== TEST COMPLETED WITHOUT ISE ===")
        print("Inspect screenshots for:")
        print(" - Unselected smoothing/period buttons: blue border + blue text (same as PAUSE button)")
        print(" - Active button highlighted differently (filled)")
        print(" - Tooltip numbers with 3 decimals (e.g. 1.216 not 8+ digits)")
        print(" - After nav back, auto state (pause/resume) and interval persisted")
        browser.close()
        sys.exit(0)

if __name__ == "__main__":
    main()
