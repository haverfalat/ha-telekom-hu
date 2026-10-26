#!/usr/bin/env python3
"""Manual official browser login, then export a Telekom-only session.

Requires `uv sync --extra browser`. SMS and CAPTCHA are completed by the user.
This helper never reads or stores passwords or SMS codes.
"""

import argparse
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from telekom_hu.web import (
    TelekomWebClient,
    party_id_from_selected_entity,
    save_web_session,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path(".telekom/web-session.json")
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--channel", default="chrome", help="Installed browser channel, e.g. chrome"
    )
    parser.add_argument(
        "--profile", type=Path, default=Path(".telekom/browser-profile")
    )
    args = parser.parse_args()
    with sync_playwright() as pw:
        args.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        context = pw.chromium.launch_persistent_context(
            str(args.profile.resolve()),
            headless=False,
            channel=args.channel,
            locale="hu-HU",
        )
        try:
            page = context.new_page()
            page.goto(
                "https://www.telekom.hu/fiokom/api/oidc-client/login?redirectUrl=/fiokom/attekintes"
            )
            print(
                "Complete login in the browser, including SMS/CAPTCHA. Waiting for an authenticated profile…",
                flush=True,
            )
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                if page.is_closed():
                    raise SystemExit("Browser closed before session verification")
                if page.url.startswith("https://www.telekom.hu/fiokom/"):
                    selected = page.evaluate(
                        "sessionStorage.getItem('mt-selected-entity')"
                    )
                    party = party_id_from_selected_entity(selected)
                    headers = {"Accept": "application/json"}
                    if party:
                        headers["x-selected-party-id"] = party
                    response = context.request.get(
                        "https://www.telekom.hu/fiokom/api/profile",
                        headers=headers,
                        max_redirects=0,
                    )
                    if response.status == 200 and "json" in response.headers.get(
                        "content-type", ""
                    ):
                        cookies = context.cookies("https://www.telekom.hu")
                        # The browser can reach profile before selecting a household.
                        # Resolve a single account and check its dashboard before closing.
                        with TelekomWebClient(cookies, party_id=party) as web:
                            try:
                                web.dashboard()
                            except ValueError:
                                page.wait_for_timeout(1500)
                                continue
                            party = web.http.headers.get("x-selected-party-id")
                        save_web_session(args.output, cookies, party_id=party)
                        print(
                            f"Authenticated session saved to {args.output}. Browser can now close.",
                            flush=True,
                        )
                        return
                page.wait_for_timeout(1500)
            raise SystemExit("Login timed out; no session exported")
        finally:
            context.close()


if __name__ == "__main__":
    main()
