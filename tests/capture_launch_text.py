#!/usr/bin/env python3
"""Capture launch: poll until server responds with full rendered UI text (not just script)."""
import os
import time
import urllib.request
from pathlib import Path

PORT = os.environ.get("PORT", "8081")
BASE = f"http://localhost:{PORT}"
LOG = Path("scratch_launch_full.log")

def main():
    print("Waiting for full render text on", BASE)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE, timeout=4) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            if "Historical Charts" in body or "NO SMOOTHING" in body or "5 LAST" in body or "Energy by Period" in body:
                print("FOUND full render markers in body")
                with open(LOG, "w", encoding="utf-8") as f:
                    f.write(body[:20000])
                print("Saved body snippet to", LOG)
                Path("launch1.log").write_text("RESPONSE BODY contains: " + ("Historical Charts" if "Historical Charts" in body else "smoothing") , encoding="utf-8")
                print("OK: launch body has rendered UI text")
                return 0
        except Exception as e:
            print("wait...", str(e)[:80])
        time.sleep(1.5)
    print("WARN: did not see full text markers in time")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
