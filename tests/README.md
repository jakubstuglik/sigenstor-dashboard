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

Tests assume the server is running at http://localhost:8080.

Screenshots are saved to the `screenshots/` directory at the project root.
