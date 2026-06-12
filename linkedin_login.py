"""Run once (and again every ~30 days) to capture a logged-in LinkedIn
session for the headless scrape in daily_run.py.

Opens a headed Chromium. Log in fully (email/password + 2FA + any Sales
Navigator prompts). When the LinkedIn home page is settled, return to this
terminal and press Enter. The browser's cookies and localStorage are
serialized into storage_state.json.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

STATE_FILE = Path(__file__).parent / "storage_state.json"


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.linkedin.com/login")

        print("Log in to LinkedIn (including any 2FA and Sales Navigator prompts).")
        print("When you see the LinkedIn home feed, come back here and press Enter.")
        input("> ")

        context.storage_state(path=str(STATE_FILE))
        browser.close()
    print(f"Saved session to {STATE_FILE}")
    print("Run `chmod 600 storage_state.json` to lock it down.")


if __name__ == "__main__":
    main()
