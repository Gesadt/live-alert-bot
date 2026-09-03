#!/usr/bin/env python3
"""
Isolated @here test -- sends the absolute minimum payload needed to test
whether @here renders as a real, highlighted, pinging mention in this
specific server/channel. No embeds, no thumbnail, no retry logic --
just this one mechanism, so a failure here can only mean one of:
  (a) the payload itself isn't reaching Discord as expected, or
  (b) something about this server/channel is still blocking it,
not "something else in the bigger script is interfering."
"""

import json
import os
import urllib.error
import urllib.request

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL is not set")

payload_dict = {
    "content": "@here isolated ping test -- if this pings/highlights, the mechanism works.",
    "allowed_mentions": {"parse": ["everyone"]},
}
payload = json.dumps(payload_dict).encode()

print(f"Sending exact payload: {json.dumps(payload_dict, indent=2)}")

# ?wait=true makes Discord return the actual created message object instead
# of a bare 204 No Content -- that response includes mention_everyone, which
# is Discord's own ground-truth record of whether this registered as a real
# mention. This is definitive; visual highlighting alone is not.
req = urllib.request.Request(
    WEBHOOK_URL + ("&" if "?" in WEBHOOK_URL else "?") + "wait=true",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode())
        print(f"Discord responded with status: {resp.status}")
        print(f"mention_everyone (this is the ground truth): {body.get('mention_everyone')}")
        print(f"mentions (users): {body.get('mentions')}")
        print(f"mention_roles: {body.get('mention_roles')}")
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="replace")
    print(f"HTTPError {e.code}: {body}")
    raise
