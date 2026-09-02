#!/usr/bin/env python3
"""
Stripchat -> Discord "went live" alert checker (production version).

How this works, confirmed via a diagnostic session:
  - The internal status API (/api/front/v2/models/username/<u>/cam) is
    specifically hardened against direct automated requests (returns
    HTTP 418) even from a real browser session. We don't use it.
  - The plain model page itself (https://stripchat.com/<username>) is
    NOT blocked or challenged — it loads real content immediately, with
    no Cloudflare "cf-mitigated" header, from a GitHub Actions IP.
  - That page embeds the exact same live/offline data the page itself
    uses to render its own LIVE badge, as JSON inside a <script> tag:
    viewCamBase.model.isLive (bool) and viewCamBase.model.status (str).
    Confirmed present and readable in both the "live" and "offline" states.

So: load the page with a real headless browser (needed so an ordinary
navigation looks and behaves like a normal visitor's), then read that
embedded JSON directly instead of calling the hardened API.

State (who was live last time we checked) is persisted to state.json so
we only alert once per "went live" event, not every run while live.
Critically: state is only flipped to "live" if the Discord alert actually
succeeds, so a failed webhook post gets retried on the next run instead
of being silently lost.

The alert's thumbnail is pulled from the page's own og:image meta tag
(the same field designed for link previews/SEO) rather than relying on
Discord's own link auto-unfurl, which was unreliable in testing.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from playwright.async_api import async_playwright

# ---- Configure your models via a GitHub Secret, not here ----
# Set a repo secret named STRIPCHAT_USERNAMES with a comma-separated list,
# e.g. "JadeScarlet_LoveLace" or "model_one,model_two". This keeps the
# actual username out of the source code entirely -- important once the
# repo is public, since source code is the first thing anyone browsing
# the repo sees.
USERNAMES = [
    u.strip() for u in os.environ.get("STRIPCHAT_USERNAMES", "").split(",") if u.strip()
]
# ---------------------------------------------------------------

STATE_FILE = "state.json"
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# Optional: a plain @here/@role ping and an accent color for the embed.
PING_TEXT = os.environ.get("DISCORD_PING", "@here")  # e.g. "@here" or a role mention, or "" for none
EMBED_COLOR = 0xFF3E7F  # pinkish-red accent; change to taste (decimal, not hex string)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# How many times to retry a failed webhook POST before giving up for this run.
WEBHOOK_RETRIES = 3
WEBHOOK_RETRY_DELAY_SECONDS = 5


def state_key(username: str) -> str:
    """A stable, non-reversible label for this username, used as the
    state.json key and in log output -- so the plaintext username never
    appears in a committed file or a (public, on a public repo) Action
    log, even though the code needs the real username in-memory to
    actually navigate to the right page and post a useful Discord alert."""
    return hashlib.sha256(username.strip().lower().encode()).hexdigest()[:12]


def find_model_state(script_texts, username):
    """Locate this model's embedded state object inside the page's own
    <script> content, using a real JSON parser (json.JSONDecoder.raw_decode)
    rather than regex, so we don't break on braces that appear inside
    string values elsewhere in the (very large) embedded config blob.

    Returns (view_cam_base, model) so callers can also inspect sibling
    fields on view_cam_base, not just the model sub-object.
    """
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
                view_cam_base = obj.get("viewCamBase") or {}
                model = view_cam_base.get("model") or {}
                if isinstance(model, dict) and model.get("username", "").lower() == username.lower():
                    return view_cam_base, model
    return None, None


async def check_live(browser, username: str):
    label = state_key(username)  # never log the plaintext username
    context = await browser.new_context(user_agent=USER_AGENT, locale="en-US")
    page = await context.new_page()
    try:
        url = f"https://stripchat.com/{username}"
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is None or response.status != 200:
            status = response.status if response else None
            print(f"[warn] {label}: unexpected HTTP status {status}")
            return None

        # Small settle time, consistent with what worked during testing.
        await page.wait_for_timeout(3000)

        script_texts = await page.eval_on_selector_all(
            "script", "els => els.map(el => el.textContent || '')"
        )
        view_cam_base, model = find_model_state(script_texts, username)
        if model is None:
            print(f"[warn] {label}: couldn't find embedded state "
                  f"(Stripchat may have changed their page structure)")
            return None

        # The image Stripchat itself designates for link previews/SEO --
        # confirmed reliable, works whether the internal app-state field
        # names change or not.
        og_image = None
        try:
            og_image = await page.get_attribute('meta[property="og:image"]', "content")
        except Exception as e:
            print(f"[warn] {label}: og:image lookup failed ({e})")

        is_live = model.get("isLive") is True
        print(f"{label}: isLive={is_live} status={model.get('status')!r}")

        model["_thumbnail_url"] = og_image  # stash for send_alert to use
        return model
    except Exception as e:
        print(f"[warn] {label}: check failed ({e})")
        return None
    finally:
        await context.close()


def send_alert(username: str, model: dict) -> bool:
    """Post a 'went live' embed to Discord. Returns True only on confirmed
    success, so the caller can decide whether it's safe to update state.

    The thumbnail is set explicitly from the page's own og:image meta tag
    (stashed on model["_thumbnail_url"] by check_live) rather than relying
    on Discord auto-unfurling a plain URL -- that was unreliable in
    testing and depends on Discord's own crawler succeeding independently.
    """
    if not WEBHOOK_URL:
        print("[error] DISCORD_WEBHOOK_URL is not set, skipping notification")
        return False

    profile_url = f"https://stripchat.com/{username}"
    embed = {
        "title": "🟢 Mommy Scarlet is live now!",
        "description": "Come say hi and show some support 💕",
        "url": profile_url,
        "color": EMBED_COLOR,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Stripchat Alerts"},
    }
    thumbnail_url = model.get("_thumbnail_url")
    if thumbnail_url:
        embed["image"] = {"url": thumbnail_url}

    content = f"{PING_TEXT} {profile_url}".strip() if PING_TEXT else profile_url
    payload = json.dumps({
        "content": content,
        "username": "Stripchat Alerts",
        "embeds": [embed],
        # Explicitly allow @everyone/@here and role pings to actually render
        # as proper highlighted mentions -- without this, whether it renders
        # correctly depends on Discord's implicit default behavior, which
        # isn't reliable to assume. This only affects what WE control via
        # DISCORD_PING (an env var/secret), not any user-generated content,
        # so broadly allowing these two types here is safe.
        "allowed_mentions": {"parse": ["everyone", "roles"]},
    }).encode()

    for attempt in range(1, WEBHOOK_RETRIES + 1):
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            print(f"[ok] alert sent for {state_key(username)}")
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            print(f"[error] attempt {attempt}/{WEBHOOK_RETRIES} failed for {state_key(username)}: "
                  f"HTTP {e.code}: {body}")
        except Exception as e:
            print(f"[error] attempt {attempt}/{WEBHOOK_RETRIES} failed for {state_key(username)}: {e}")

        if attempt < WEBHOOK_RETRIES:
            time.sleep(WEBHOOK_RETRY_DELAY_SECONDS)

    print(f"[error] giving up on alert for {state_key(username)} after {WEBHOOK_RETRIES} attempts; "
          f"state will NOT be marked live, so this will retry next run")
    return False


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


async def main():
    if not USERNAMES:
        print("[error] STRIPCHAT_USERNAMES secret is empty or not set")
        sys.exit(1)

    is_first_run = not os.path.exists(STATE_FILE)
    if is_first_run:
        print("[info] no state.json found -- this looks like the first run. "
              "Any models already live right now will be recorded silently "
              "(no alert) so we don't ping for a stream that may have "
              "already been running for a while before tracking started.")

    state = load_state()
    dirty = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for username in USERNAMES:
            key = state_key(username)
            model = await check_live(browser, username)
            if model is None:
                continue  # leave prior known state untouched on a failed check

            is_live = model.get("isLive") is True
            was_live = state.get(key, False)

            if is_live and not was_live:
                if is_first_run:
                    # Don't alert on the very first observation -- just
                    # record it, so we don't fire a stale "went live" for
                    # a stream that started before we were watching.
                    state[key] = True
                    dirty = True
                else:
                    alerted = send_alert(username, model)
                    if alerted:
                        state[key] = True
                        dirty = True
                    # else: leave state as-is (not live) so this retries
                    # again next run instead of being silently lost.
            elif (not is_live) and was_live:
                state[key] = False
                dirty = True
            # else: no transition, nothing to do.

        await browser.close()

    if dirty:
        save_state(state)


if __name__ == "__main__":
    asyncio.run(main())
