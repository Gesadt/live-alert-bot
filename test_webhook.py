#!/usr/bin/env python3
"""
Standalone webhook test — sends one test message immediately, regardless
of any model's live status. Run this to confirm DISCORD_WEBHOOK_URL is
wired up correctly, separate from whether a model happens to be live.
"""

import json
import os
import urllib.error
import urllib.request

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

if not WEBHOOK_URL:
    raise SystemExit("DISCORD_WEBHOOK_URL is not set")

payload = json.dumps({
    "content": "✅ Test message — if you see this, the webhook is wired up correctly.",
    "username": "Stripchat Alerts",
}).encode()

req = urllib.request.Request(
    WEBHOOK_URL,
    data=payload,
    headers={
        "Content-Type": "application/json",
        # Python's default urllib User-Agent ("Python-urllib/3.x") is
        # widely blocklisted as a generic-scraper signature. A normal
        # browser-style User-Agent avoids that entirely.
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
    },
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"Discord responded with status: {resp.status}")
except urllib.error.HTTPError as e:
    body = e.read().decode(errors="ignore")
    print(f"HTTPError {e.code}: {body}")
    raise
