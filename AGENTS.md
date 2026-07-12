# AGENTS.md - Important Rules for Working on SigenStor Dashboard

## Mandatory: Always Test Your Changes with Playwright (No Exceptions)
**You must test EVERY change you make using the full procedure below before claiming it is done.** This is non-negotiable. If the user reports an internal server error, blank UI, or broken feature, reproduce it immediately with a fresh server start + Playwright navigation + interaction + screenshots + read_file inspection + log check. Fix the root cause, re-test the entire flow (including clicking the new controls like smoothing buttons), and loop until the UI renders cleanly with no 500 errors and the feature works visually. Document lessons learned by updating this file. Never rely on "it looks correct in code" or previous test runs.

## Mandatory Testing of All Changes (Playwright + Visual Inspection Loop)
**Testing your changes with Playwright is MANDATORY for every modification.** Never skip this step. After editing code (especially UI, state, buttons, toggles, or handlers that affect the Charts page or config persistence):

1. Start/restart the server in background or with monitor using dev isolation ONLY: `PORT=8081 SIGENSTOR_DB=data/sigenstor_dev.db python main.py` (venv python; NEVER 8080 or prod DB file).
2. Use a Playwright script (python -c or temp_test_playwright.py or test_ui.py) to:
   - Navigate to http://localhost:8081 (or use hamburger on narrow viewport)
   - Click "CHARTS" in sidebar
   - Interact with the new controls: the Power series switches (toggles), AND the new smoothing buttons ("No smoothing", "3 last", "5 last").
   - Click period buttons too.
   - Observe if charts update without Internal Server Error (500), blank areas, or crashes.
3. Save full_page screenshots after navigation and after clicking each new button (e.g. screenshots/charts_after_5_last.png).
4. Immediately use the `read_file` tool on the .png screenshot files — the tool will use a multimodal model to describe exactly what is rendered (look for the buttons on the right of the power toggles row, highlighted active smoothing button with blue, no error messages, charts visible and updating).
5. Check the server output/logs (from the background task or logs/sigenstor_*.log) for any exceptions, tracebacks, or "Internal Server Error".
6. If error (e.g. ISE, AttributeError, NameError, stale element, or buttons not appearing/clickable), diagnose (read code + logs + screenshot description), fix, re-start server, re-run playwright test, re-read screenshots, repeat the loop **until no errors and visual confirmation that it works**.
7. Only after successful visual + log confirmation in the loop, consider the change complete. Update this AGENTS.md with any new gotchas found.

**This is non-negotiable.** Historical problems like the global declaration order causing SyntaxError (leading to ISE on startup), stale closures breaking auto-refresh, or layout/button issues only appear at runtime. Always loop the test-fix cycle.

## Mandatory Testing Procedure for Every Change
**You MUST test every code change yourself before considering it done:**

1. Run the server with dev isolation: `PORT=8081 SIGENSTOR_DB=data/sigenstor_dev.db python main.py` (use venv python; use monitor or background; NEVER 8080 or prod DB).
2. Use Playwright (via python -c script or test_ui.py, with BASE_URL from PORT or 8081) to:
   - Open http://localhost:8081
   - Navigate by clicking sidebar items (DASHBOARD, CHARTS, SUMMARY, RAW DATA, SETTINGS) or hamburger on mobile.
   - Interact with controls: click range buttons, Apply, Refresh, form fields, smoothing, etc.
   - Wait for async updates (timers, loads, plots).
3. Save screenshots after key actions (e.g. `page.screenshot(path='screenshots/after_charts_click.png', full_page=True)`).
4. Use the `read_file` tool on the .png files (it uses multimodal LLM to describe the visual content of the UI).
5. Check server logs (via monitor events or `logs/sigenstor_*.log`) for errors like slot issues, ValueError, AttributeError on None, etc.
6. Only declare success after you have visually confirmed via screenshots + descriptions that the feature works as expected (no blank pages, controls in right place, no crashes on click, data loads, etc.).

**Never assume a change "should work" based on code inspection alone.** Historical bugs (timer closures, NiceGUI slot/context errors when updating UI from background timers/tasks, element creation order affecting layout, invalid select values, None references) only surface when actually running and clicking.

## Common Gotchas Observed
- NiceGUI `ui.timer(...)` callbacks and `await` loads from them often need strong try/except guards because containers can become stale after navigation.
- Element creation order in NiceGUI columns/rows determines visual position — declare controls first if they must appear at top.
- `ui.select(value=xxx)` will raise ValueError if xxx not in options at construction time.
- Global/module state for intervals, containers, and status labels can lead to None or stale closures if not rebound before use and if not guarded.
- Multiple visits to a page can accumulate timers unless you guard creation with a flag (e.g. `_charts_auto_timer_started`).
- Always re-test after any refactor to show_*/load functions or timer logic.

## Other Notes
- When user reports "X doesn't work again", the first action is to reproduce with the full run + playwright + screenshot inspection cycle.
- **Always visually inspect changes**: After every UI modification, use Playwright to capture screenshots of the affected components (e.g. gauges, charts, labels) and use `read_file` on the .png to describe the rendered result. Ensure no text clipping, sufficient spacing/gaps from edges/lines, readable font sizes (as specified: e.g. percent bigger than kWh), proper alignment, no overlaps, and that the UI looks functional and aesthetic (not cramped, titles fully visible, consistent styling). If it doesn't look good, iterate immediately. Add this as a mandatory step in testing.
- Update this file with new lessons as they are discovered through testing.
- **Visual QA is mandatory**: Never assume changes "look good" from code. Always capture screenshots of the exact UI component (gauges, labels, etc.) after edits, use `read_file` on the PNG to inspect for clipping (e.g. titles), font sizes (e.g. make percent larger than kWh), spacing, and overall aesthetics/legibility. Fix until it is functional and looks professional. Add explicit reminders here for future.
- **Dev isolation for testing (per goal constraints)**: NEVER use port 8080 or the prod data/sigenstor.db when developing/testing. Always run `PORT=8081 SIGENSTOR_DB=data/sigenstor_dev.db python main.py` (using venv python). Seed dev DB from prod snapshot when needed. This keeps the running prod container untouched. Update this file if new gotchas with 8081 flows or separate DB are found. All playwright verification in this work used 8081+dev DB.