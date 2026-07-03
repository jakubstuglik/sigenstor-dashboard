#!/usr/bin/env python3
"""
UI Test + Screenshot tool for SigenStor Dashboard.
Uses Playwright to:
- Wait for server
- Visit pages by clicking sidebar
- Take screenshots of each major view
- Optionally wait and re-screenshot to check refresh/staleness
"""

import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SCREENSHOTS_DIR = Path("screenshots")
SCREENSHOTS_DIR.mkdir(exist_ok=True)

BASE_URL = "http://localhost:8080"

def wait_for_server(page, timeout=30):
    print("Waiting for server to be ready...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=5000)
            # Look for the title or sidebar text
            if page.locator("text=SigenStor").first.is_visible() or "Dashboard" in page.title():
                print("Server ready!")
                return True
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("Server did not become ready")

def take_screenshot(page, name: str):
    path = SCREENSHOTS_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"Saved: {path}")
    return path

def click_nav(page, label: str):
    # Sidebar buttons have text like DASHBOARD, CHARTS etc.
    # Try exact role or text match
    try:
        # Try button with text
        btn = page.locator(f"button:has-text('{label}')").first
        if btn.count() > 0:
            btn.click()
            time.sleep(1.2)  # allow rebuild
            return
    except:
        pass
    # Fallback to text
    page.locator(f"text={label}").first.click()
    time.sleep(1.2)

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # headless for CI-like, change to False for visible if display allows
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()

        wait_for_server(page)

        # 1. Dashboard
        print("=== Capturing DASHBOARD ===")
        # Ensure on dashboard
        click_nav(page, "DASHBOARD")
        time.sleep(2)
        take_screenshot(page, "01_dashboard_initial")

        # Wait a bit for live update / poll
        print("Waiting 18s to observe refresh (to check staleness)...")
        time.sleep(18)
        take_screenshot(page, "02_dashboard_after_refresh")

        # 2. Charts
        print("=== Capturing CHARTS ===")
        click_nav(page, "CHARTS")
        time.sleep(2)
        take_screenshot(page, "03_charts")

        # Try changing range
        try:
            page.locator("text=Last 1h").first.click()
            time.sleep(1.5)
            take_screenshot(page, "04_charts_1h")
        except Exception as e:
            print("Could not interact with chart ranges:", e)

        # 3. Summary
        print("=== Capturing SUMMARY ===")
        click_nav(page, "SUMMARY")
        time.sleep(2)
        take_screenshot(page, "05_summary")

        # 4. Raw Data
        print("=== Capturing RAW DATA ===")
        click_nav(page, "RAW DATA")
        time.sleep(2)
        take_screenshot(page, "06_raw_data")

        # Try export? (will download, but we can skip actual file check)
        try:
            page.locator("text=Export CSV").first.click()
            time.sleep(1)
        except:
            pass

        # 5. Settings
        print("=== Capturing SETTINGS ===")
        click_nav(page, "SETTINGS")
        time.sleep(2)
        take_screenshot(page, "07_settings")

        # Try the Test Connection button
        try:
            page.locator("text=Test Connection").first.click()
            time.sleep(3)
            take_screenshot(page, "08_settings_after_test")
        except Exception as e:
            print("Test button interaction issue:", e)

        browser.close()
        print("\nAll screenshots captured in ./screenshots/")
        print("Now use read_file on the PNGs to inspect visuals.")

if __name__ == "__main__":
    main()
