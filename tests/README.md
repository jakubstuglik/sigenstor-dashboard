# Tests

This directory contains Playwright-based end-to-end tests for the SigenStor Dashboard.

## Prerequisites

```bash
pip install -r requirements-dev.txt
playwright install
```

## Running the tests

Start the dashboard first:

```bash
python main.py
# or
docker compose up
```

Then in another terminal:

```bash
python tests/playwright_summary_test.py
python tests/playwright_mandatory_test.py
python tests/playwright_button_highlight_test.py
python tests/playwright_test_changes.py
```

Tests assume the server is running at http://localhost:8081 with SIGENSTOR_DB=data/sigenstor_dev.db for development and verification (prod container runs on 8080 with data/sigenstor.db and must never be touched). Always start the dev server first:

```bash
$env:PORT=8081; $env:SIGENSTOR_DB='data/sigenstor_dev.db'; python main.py
```

Then run tests. The verification scripts below are designed to be run against the dev instance.

Screenshots are saved to the `screenshots/` directory at the project root.

## Verification Scripts (recommended for regression)

These scripts provide strong coverage for the major features and were used heavily during development:

- `tests/verify_full_playwright.py` — Full desktop + mobile flow (smoothing, energy panels, settings layout, drawer).
- `tests/verify_mobile_clean.py` — Focused mobile hamburger + drawer behavior.
- `tests/test_mobile_autoclose.py` — Ensures the mobile menu closes automatically after selecting a category.
- `tests/test_step6_split_real.py` — Verifies the split Energy by Period charts with real data and independent scales.
- `tests/capture_launch_text.py` — Helper to capture launch HTML containing rendered static text.

Run them with the dev environment variables as shown above. They print VERIFIED messages and save evidence screenshots.
