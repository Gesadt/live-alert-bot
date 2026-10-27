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
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone

from playwright.async_api import async_playwright

# ---- Configure your models via a GitHub Secret, not here ----
# Set a repo secret named STRIPCHAT_USERNAMES with a comma-separated list,
# e.g. "model_one" or "model_one,model_two". This keeps the
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
            title = await page.title()
            html_len = len(await page.content())
            print(f"[warn] {label}: couldn't find embedded state "
                  f"(Stripchat may have changed their page structure, or this "
                  f"was a transient load issue). page_title={title!r} "
                  f"html_length={html_len} script_blocks_found={len(script_texts)}")
            return None

        # The image Stripchat itself designates for link previews/SEO --
        # confirmed reliable, works whether the internal app-state field
        # names change or not.
        og_image = None
        try:
            og_image = await page.get_attribute('meta[property="og:image"]', "content")
        except Exception as e:
            print(f"[warn] {label}: og:image lookup failed ({e})")

        raw_is_live = model.get("isLive") is True
        status = model.get("status")
        # Only alert for genuinely public live status. isLive can be True
        # while status is something like "p2p" (a private 1-on-1) or
        # "exclusive" -- those aren't watchable by a general Discord
        # audience, so they shouldn't trigger a "come watch" alert even
        # though the model is technically broadcasting.
        is_live = raw_is_live and status == "public"
        print(f"{label}: isLive={raw_is_live} status={status!r} -> counts_as_live={is_live}")

        model["_thumbnail_url"] = og_image  # stash for send_alert to use
        model["_counts_as_live"] = is_live  # stash so main() uses the same logic
        return model
    except Exception as e:
        print(f"[warn] {label}: check failed ({e})")
        return None
    finally:
        await context.close()


SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024  # stay well under Discord's webhook upload limit
SNAPSHOT_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def download_snapshot(url):
    """Download the stream's snapshot so we can upload it to Discord ourselves.

    Returns (bytes, extension) or None on any failure. Discord only *links*
    to a remote embed image and re-fetches it through its proxy; once the
    source URL expires (Stripchat snapshots are short-lived), old messages
    lose the picture. Uploading the bytes as an attachment makes Discord
    host the file itself, so it stays visible permanently.
    """
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Referer": "https://stripchat.com/",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            ctype = (resp.headers.get_content_type() or "").lower()
            data = resp.read(SNAPSHOT_MAX_BYTES + 1)
        if ctype not in SNAPSHOT_EXTENSIONS:
            print(f"[warn] snapshot download: unexpected content type {ctype!r}")
            return None
        if not data or len(data) > SNAPSHOT_MAX_BYTES:
            print(f"[warn] snapshot download: bad size ({len(data)} bytes)")
            return None
        return data, SNAPSHOT_EXTENSIONS[ctype]
    except Exception as e:
        print(f"[warn] snapshot download failed ({e}); falling back to remote image URL")
        return None


def build_multipart(payload_obj, file_name, file_bytes, file_type):
    """Build a multipart/form-data body: payload_json + files[0]."""
    boundary = "----alertbot" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts = [
        b"--" + boundary.encode(),
        b'Content-Disposition: form-data; name="payload_json"',
        b"Content-Type: application/json",
        b"",
        json.dumps(payload_obj).encode(),
        b"--" + boundary.encode(),
        f'Content-Disposition: form-data; name="files[0]"; filename="{file_name}"'.encode(),
        f"Content-Type: {file_type}".encode(),
        b"",
        file_bytes,
        b"--" + boundary.encode() + b"--",
        b"",
    ]
    return crlf.join(parts), f"multipart/form-data; boundary={boundary}"


def send_alert(username: str, model: dict) -> bool:
    """Post a 'went live' embed to Discord. Returns True only on confirmed
    success, so the caller can decide whether it's safe to update state.

    The snapshot comes from the page's own og:image meta tag (stashed on
    model["_thumbnail_url"] by check_live). We download it at alert time and
    upload it to Discord as an attachment so Discord hosts the image
    permanently; if the download fails we fall back to linking the remote
    URL (which works for a while, then expires) rather than skipping the alert.
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
    snapshot = download_snapshot(thumbnail_url)
    attachment = None
    if snapshot:
        data, ext = snapshot
        file_name = f"snapshot.{ext}"
        embed["image"] = {"url": f"attachment://{file_name}"}
        attachment = (file_name, data, f"image/{'jpeg' if ext == 'jpg' else ext}")
        print(f"[info] snapshot downloaded ({len(data)} bytes), uploading as attachment")
    elif thumbnail_url:
        embed["image"] = {"url": thumbnail_url}

    content = f"{PING_TEXT} {profile_url}".strip() if PING_TEXT else profile_url
    payload_obj = {
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
    }
    if attachment:
        body, content_type = build_multipart(payload_obj, *attachment)
    else:
        body, content_type = json.dumps(payload_obj).encode(), "application/json"

    for attempt in range(1, WEBHOOK_RETRIES + 1):
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=body,
            headers={
                "Content-Type": content_type,
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

    if not os.path.exists(STATE_FILE):
        print("[info] no state.json found -- this looks like the very first run.")

    state = load_state()
    dirty = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for username in USERNAMES:
            key = state_key(username)
            model = await check_live(browser, username)
            if model is None:
                continue  # leave prior known state untouched on a failed check

            is_live = model.get("_counts_as_live", False)
            is_new_key = key not in state
            was_live = state.get(key, False)

            if is_new_key:
                # First time we've ever seen THIS username (true first run,
                # or a username added later) -- just record its current
                # status without alerting, so we don't ping for a stream
                # that may have already been running before we started
                # watching it.
                print(f"{state_key(username)}: first observation, isLive={is_live} "
                      f"-- recording without alerting")
                state[key] = is_live
                dirty = True
            elif is_live and not was_live:
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
