#!/usr/bin/env python3
"""
Status diagnostic ONLY -- not the production monitor.

Dumps the ENTIRE raw model object (not just isLive/status) for one
username, so we can see the true field values during each real session
type -- offline, public, exclusive, private/p2p, group, etc. -- instead
of assuming what a status string is literally called.

Run this manually at different times (once while offline, once during a
normal public show, once during whatever "exclusive" mode looks like on
the model's end) and compare the printed status value each time.
"""

import asyncio
import json
import os

from playwright.async_api import async_playwright

USERNAME = os.environ.get("STRIPCHAT_USERNAMES", "").split(",")[0].strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def find_model_state(script_texts, username):
    decoder = json.JSONDecoder()
    marker = '"viewCamBase"'
    for text in script_texts:
        if not text or marker not in text:
            continue
        marker_idx = text.find(marker)
        brace_positions = [i for i in range(marker_idx, -1, -1) if text[i] == "{"]
        for brace_idx in brace_positions:
            try:
                obj, _ = decoder.raw_decode(text, brace_idx)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "viewCamBase" in obj:
                model = (obj.get("viewCamBase") or {}).get("model") or {}
                if isinstance(model, dict) and model.get("username", "").lower() == username.lower():
                    return model
    return None


async def main():
    if not USERNAME:
        raise SystemExit("STRIPCHAT_USERNAMES is not set")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = await context.new_page()

        url = f"https://stripchat.com/{USERNAME}"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"HTTP status: {response.status if response else None}")

        await page.wait_for_timeout(3000)

        script_texts = await page.eval_on_selector_all(
            "script", "els => els.map(el => el.textContent || '')"
        )
        model = find_model_state(script_texts, USERNAME)

        if model is None:
            print("Couldn't find embedded model state this run.")
        else:
            print("Full raw model object:")
            print(json.dumps(model, indent=2, sort_keys=True))
            print()
            print(f"--> isLive:  {model.get('isLive')!r}")
            print(f"--> status:  {model.get('status')!r}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
