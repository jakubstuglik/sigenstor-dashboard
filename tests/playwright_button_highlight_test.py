#!/usr/bin/env python3
"""Targeted test for period and smoothing button active highlight."""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

# Resolve screenshots relative to project root (works when run from root or from tests/)
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "tests" else SCRIPT_DIR
SCREENSHOTS_DIR = PROJECT_ROOT / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)
BASE_URL = "http://localhost:8080"

def take_screenshot(page, name: str):
    path = SCREENSHOTS_DIR / f"btn_highlight_{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"[SHOT] {path}")
    return path

def main():
    print("=== Button highlight test ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()

        # Wait
        for _ in range(30):
            try:
                page.goto(BASE_URL, timeout=5000)
                if page.locator("text=SigenStor").first.is_visible(timeout=1000):
                    break
            except:
                pass
            time.sleep(0.5)

        # Go to CHARTS
        try:
            page.locator("button:has-text('CHARTS')").first.click()
        except:
            page.locator("text=CHARTS").first.click()
        time.sleep(2.5)
        take_screenshot(page, "01_initial")

        # Test period buttons
        print("Testing period buttons...")
        for pbtn in ["Last 6h", "Last 24h", "Last 1h"]:
            try:
                page.locator(f"button:has-text('{pbtn}')").first.click()
                time.sleep(1.0)
                print(f"  clicked {pbtn}")
                take_screenshot(page, f"02_period_{pbtn.replace(' ','_')}")
            except Exception as e:
                print(f"  err {pbtn}: {e}")

        # Test smoothing
        print("Testing smoothing buttons...")
        for sbtn in ["3 last", "5 last", "No smoothing"]:
            try:
                btn = page.locator(f"button:has-text('{sbtn}')").first
                btn.click()
                time.sleep(1.2)
                print(f"  clicked {sbtn}")
                safe = sbtn.replace(" ", "_")
                take_screenshot(page, f"03_smoothing_{safe}")
            except Exception as e:
                print(f"  err {sbtn}: {e}")

        # Final state: 1h + No smoothing
        page.locator("button:has-text('Last 1h')").first.click()
        time.sleep(0.8)
        page.locator("button:has-text('No smoothing')").first.click()
        time.sleep(1.0)
        take_screenshot(page, "04_final_1h_no_smooth")

        browser.close()
        print("Done. Inspect the btn_highlight_*.png files for filled blue on active buttons.")
        print("Active should be solid blue (like APPLY), unselected should have blue border/text.")

if __name__ == "__main__":
    main()
