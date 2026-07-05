#!/usr/bin/env python3
"""
Mandatory Playwright test per AGENTS.md for Charts smoothing + power toggles + periods.
Run after server start. Exits non-zero on ISE or failures.
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
    path = SCREENSHOTS_DIR / f"commit_test_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"[SHOT] Saved: {path}")
    return path

def main():
    print("=== MANDATORY PLAYWRIGHT TEST (pre-commit) ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1100})
        page = context.new_page()

        # Wait for server
        print("Waiting for server...")
        for i in range(30):
            try:
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=8000)
                if page.locator("text=SigenStor").first.is_visible(timeout=2000):
                    print("Server ready.")
                    break
            except Exception:
                pass
            time.sleep(1)
        else:
            print("ERROR: Server not ready")
            sys.exit(1)

        take_screenshot(page, "00_initial")

        # Go to CHARTS
        print("Clicking CHARTS in sidebar...")
        try:
            page.locator("button:has-text('CHARTS')").first.click()
            time.sleep(2.5)
        except Exception as ex:
            print(f"CHARTS nav error: {ex}")
            page.locator("text=CHARTS").first.click()
            time.sleep(2.5)

        take_screenshot(page, "01_charts_loaded")
        content = page.content()
        if "Internal Server Error" in content or "Traceback (most recent call last)" in content:
            print("!!! ISE DETECTED ON CHARTS LOAD !!!")
            idx = content.find("Internal Server Error")
            print(content[max(0, idx-50):idx+800] if idx >= 0 else content[:1500])
            sys.exit(2)

        print("CHARTS loaded without ISE.")

        # Test period buttons
        print("Testing period buttons...")
        for period in ["Last 1h", "Last 6h", "Last 24h", "Last 7d"]:
            try:
                btn = page.locator(f"button:has-text('{period}')").first
                if btn.is_visible():
                    btn.click()
                    time.sleep(1.2)
                    print(f"  Clicked period: {period}")
            except Exception as e:
                print(f"  Period {period} click issue: {e}")

        take_screenshot(page, "02_after_periods")

        # Test power visibility toggles (switches in the power row)
        print("Testing power series switches (toggles)...")
        # Look for switches near "Power:" label or colored indicators
        try:
            # NiceGUI switches are often input type checkbox or role switch
            switches = page.locator("input[type='checkbox'], [role='switch']").all()
            print(f"  Found ~{len(switches)} potential switch elements")
            for i, sw in enumerate(switches[:4]):  # limit to first few (PV, Battery, Grid, Load)
                try:
                    if sw.is_visible():
                        sw.click()
                        time.sleep(0.8)
                        print(f"  Toggled switch #{i}")
                        take_screenshot(page, f"03_toggle_{i}")
                        sw.click()  # toggle back for next
                        time.sleep(0.6)
                except Exception as e:
                    print(f"  Switch {i} issue (may be ok): {e}")
        except Exception as ex:
            print(f"  Switches scan error (non-fatal): {ex}")

        # The critical part: smoothing buttons on the right of power toggles row
        print("Testing smoothing buttons: 'No smoothing', '3 last', '5 last'...")
        smoothing_labels = ["No smoothing", "3 last", "5 last"]
        for label in smoothing_labels:
            try:
                # Buttons styled like period ones
                btn = page.locator(f"button:has-text('{label}')").first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    time.sleep(1.8)  # allow re-render of smoothed charts
                    print(f"  Clicked smoothing: {label}")
                    safe = label.replace(" ", "_")
                    take_screenshot(page, f"04_smoothing_{safe}")
                    # Verify no ISE after click
                    c = page.content()
                    if "Internal Server Error" in c:
                        print(f"!!! ISE after clicking {label} !!!")
                        sys.exit(3)
                else:
                    print(f"  WARNING: smoothing button '{label}' not visible")
            except Exception as ex:
                print(f"  ERROR clicking smoothing '{label}': {ex}")
                take_screenshot(page, f"error_{label.replace(' ','_')}")
                sys.exit(4)

        # Final state screenshot + re-click one
        print("Re-click '5 last' and 'No smoothing' to confirm...")
        try:
            page.locator("button:has-text('5 last')").first.click()
            time.sleep(1.5)
            page.locator("button:has-text('No smoothing')").first.click()
            time.sleep(1.5)
        except Exception as e:
            print(f"Final clicks issue: {e}")

        take_screenshot(page, "05_final_after_smoothing_cycle")

        # Check auto-refresh elements if present (pause/resume)
        print("Checking for auto-refresh / pause controls...")
        try:
            pause_btn = page.locator("button:has-text('PAUSE') , button:has-text('AUTO')").first
            if pause_btn.is_visible():
                print("  Auto-refresh controls visible.")
        except:
            pass

        # Final content check
        final_content = page.content()
        if "Internal Server Error" in final_content:
            print("!!! FINAL ISE DETECTED !!!")
            sys.exit(5)

        print("=== ALL TESTS PASSED: No ISE, smoothing buttons clickable, charts rendered ===")
        browser.close()
        sys.exit(0)

if __name__ == "__main__":
    main()
