# AGENTS.md - Important Rules for Working on SigenStor Dashboard

## Mandatory Testing Procedure for Every Change
**You MUST test every code change yourself before considering it done:**

1. Run the server: `python main.py` (use the monitor tool or background process to keep it alive).
2. Use Playwright (via python -c script or test_ui.py) to:
   - Open http://localhost:8080
   - Navigate by clicking sidebar items (DASHBOARD, CHARTS, SUMMARY, RAW DATA, SETTINGS)
   - Interact with controls: click range buttons, Apply, Refresh, form fields, etc.
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
- Update this file with new lessons as they are discovered through testing.