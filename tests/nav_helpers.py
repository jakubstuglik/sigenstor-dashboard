#!/usr/bin/env python3
"""
Shared nav helpers for reliable Playwright flows on SigenStor dashboard.
Uses data-* attributes added to shipped controls for stability.
"""

from playwright.sync_api import Page, expect, TimeoutError as PWTimeout

def ensure_drawer_open(page: Page, viewport_width: int = 1400):
    """Ensure the category nav is reachable.
    On wide viewports drawer should be persistent (via breakpoint/show-if-above).
    On narrow, click the menu-toggle if present and closed.
    """
    # Try to find a visible nav button
    try:
        page.locator('[data-nav="charts"]').first.wait_for(state="visible", timeout=1500)
        return  # already open/persistent
    except PWTimeout:
        pass

    # On mobile/narrow: click hamburger
    try:
        ham = page.locator('button.menu-toggle, button[aria-label="Open menu"]').first
        ham.wait_for(state="visible", timeout=2000)
        ham.click(timeout=2000)
        page.wait_for_timeout(600)
    except Exception:
        # fallback: any top menu-ish button
        try:
            page.locator('button.q-mr-sm, header button').first.click(timeout=1500)
            page.wait_for_timeout(600)
        except:
            pass

def click_nav(page: Page, name: str):
    """Click a nav item by data-nav or text. Waits for visible."""
    sel = f'[data-nav="{name.lower()}"]'
    try:
        btn = page.locator(sel).first
        btn.wait_for(state="visible", timeout=3000)
        btn.click(timeout=3000)
    except PWTimeout:
        # fallback to text
        page.locator("button", has_text=name).first.click(timeout=3000, force=True)
    page.wait_for_timeout(800)

def assert_on_page(page: Page, marker: str, timeout: int = 5000):
    """Assert we are on the expected page by waiting for a marker (text or data attr)."""
    if marker.startswith("data-"):
        expect(page.locator(f"[{marker}]").first).to_be_visible(timeout=timeout)
    else:
        expect(page.get_by_text(marker, exact=False).first).to_be_visible(timeout=timeout)

def click_smoothing(page: Page, label: str):
    """Click smoothing button by data attr or text."""
    s_map = {"No smoothing": "0", "3 last": "3", "5 last": "5"}
    s = s_map.get(label, "")
    try:
        btn = page.locator(f'[data-smoothing="{s}"]').first
        btn.wait_for(state="visible", timeout=3000)
        btn.click(timeout=3000)
    except PWTimeout:
        page.locator("button", has_text=label).first.click(timeout=3000, force=True)
    page.wait_for_timeout(900)
