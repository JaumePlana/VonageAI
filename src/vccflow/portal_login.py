"""Interactive portal login: open a browser, let the user sign in, save the session cookies."""

from __future__ import annotations

import json
import time

from vccflow.config import Account
from vccflow.portal import SESSIONS_DIR, session_file

LOGIN_TIMEOUT_S = 300


def login(account: Account, browser_channel: str = "msedge") -> str:
    """Return the path of the saved session file."""
    from playwright.sync_api import sync_playwright

    start_url = f"{account.portal_url}/apps/applications.asp"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=browser_channel, headless=False)
        # A fresh context per login, so sessions of different accounts never mix.
        context = browser.new_context()
        page = context.new_page()
        page.goto(start_url)
        print(f"Sign in to the '{account.name}' account in the browser window...")

        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        while time.monotonic() < deadline:
            url = page.url
            if url.startswith(account.portal_url) and "/Auth/" not in url:
                break
            page.wait_for_timeout(1000)
        else:
            browser.close()
            raise TimeoutError(f"Login not completed within {LOGIN_TIMEOUT_S}s")

        page.wait_for_load_state("networkidle")
        state = context.storage_state()
        browser.close()

    SESSIONS_DIR.mkdir(exist_ok=True)
    path = session_file(account)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return str(path)
