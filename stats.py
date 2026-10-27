#!/usr/bin/env python3
"""
Stream statistics: per-stream "ended, lasted X" messages and a weekly report.

Design notes
------------
* Data lives in a SEPARATE PRIVATE GitHub repo (file: data.json), read and
  written through the GitHub Contents API with a fine-grained token. It is
  deliberately NOT kept in this (public) repo, because timestamps of
  streams would publish the model's schedule. Nothing in this module ever
  prints a timestamp, duration, repo name or token to the (public) log.
* Everything here is best-effort and optional. If the secrets aren't set,
  or GitHub/Discord errors out, stats are skipped and alerts are unaffected.
* If the data file can't be LOADED, it is never written (so a transient
  error can't wipe the history).

How a "session" is measured
---------------------------
The monitor only sees snapshots (one every ~10 minutes). Each snapshot is
recorded as public ("p"), private/p2p/exclusive ("x") or offline ("o").
Time between two snapshots is split at the midpoint; half a check interval
is assumed before the first live snapshot, and (up to) half after the last
one. Result: durations are unbiased estimates, accurate to roughly one
check interval in total.

A stream that drops and returns within STATS_MERGE_GAP_MIN minutes is one
session (offline time inside it is not counted). A session is only
"finished" -- and announced -- once she has stayed offline for that long.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DATA_PATH = "data.json"
ASSUMED_INTERVAL_S = int(os.environ.get("STATS_CHECK_INTERVAL_MIN", "10")) * 60
MERGE_GAP_S = int(os.environ.get("STATS_MERGE_GAP_MIN", "20")) * 60
MERGE_TOLERANCE_S = 60  # run-timing jitter allowance
TZ = ZoneInfo(os.environ.get("STATS_TIMEZONE") or "Europe/Vilnius")
STATE_REPORT_KEY = "__last_report_week"  # stored in state.json (not sensitive)
ENDED_RETRY_WINDOW = timedelta(hours=48)

COLOR_ENDED = 0x747F8D
COLOR_REPORT = 0x5865F2


# ---------------------------------------------------------------- helpers
def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_duration(seconds):
    """Rounded to the nearest 5 minutes -- more precision would be false."""
    minutes = int(round(seconds / 300.0)) * 5
    if seconds > 0 and minutes < 5:
        minutes = 5
    h, m = divmod(minutes, 60)
    if h:
        return f"~{h}h {m:02d}m"
    return f"~{m}m"


def post_json(url, payload, retries=3, delay=5):
    body = json.dumps(payload).encode()
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            return True
        except urllib.error.HTTPError as e:
            print(f"[stats] webhook attempt {attempt}/{retries} failed: HTTP {e.code}")
        except Exception as e:
            print(f"[stats] webhook attempt {attempt}/{retries} failed: {type(e).__name__}")
        if attempt < retries:
            time.sleep(delay)
    return False


def compute_session(obs, offline_since):
    """obs: list of (datetime, 'p'|'x'|'o'), first and last are live.
    Returns dict with start/end (datetimes) and public/private seconds."""
    half = ASSUMED_INTERVAL_S / 2.0
    totals = {"p": 0.0, "x": 0.0}

    def add(cls, secs):
        if cls in totals and secs > 0:
            totals[cls] += secs

    obs = sorted(obs, key=lambda o: o[0])
    add(obs[0][1], half)
    for (t1, c1), (t2, c2) in zip(obs, obs[1:]):
        gap = (t2 - t1).total_seconds()
        add(c1, gap / 2.0)
        add(c2, gap / 2.0)
    t_last, c_last = obs[-1]
    trail = max(0.0, min((offline_since - t_last).total_seconds() / 2.0, ASSUMED_INTERVAL_S))
    add(c_last, trail)
    return {
        "start": obs[0][0] - timedelta(seconds=half),
        "end": t_last + timedelta(seconds=trail),
        "public_s": totals["p"],
        "private_s": totals["x"],
    }


# ------------------------------------------------------------------ class
class Stats:
    def __init__(self, repo, token, webhook_url):
        self.repo = repo
        self.token = token
        self.webhook_url = webhook_url
        self.api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
        self.data = {"active": {}, "sessions": []}
        self.sha = None
        self.dirty = False
        self.loaded = False

    @classmethod
    def from_env(cls):
        repo = os.environ.get("STATS_REPO", "").strip()
        token = os.environ.get("STATS_REPO_TOKEN", "").strip()
        webhook = (os.environ.get("DISCORD_STATS_WEBHOOK_URL", "").strip()
                   or os.environ.get("DISCORD_WEBHOOK_URL", "").strip())
        if not repo or not token:
            return None
        return cls(repo, token, webhook)

    # ---- GitHub Contents API ------------------------------------------
    def _call(self, method, payload=None):
        url = f"{self.api}/repos/{self.repo}/contents/{DATA_PATH}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "live-alert-bot",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    def load(self):
        try:
            code, obj = self._call("GET")
        except Exception as e:
            print(f"[stats] couldn't reach GitHub ({type(e).__name__}) -- skipping stats this run")
            return False
        if code == 404:
            self.data, self.sha = {"active": {}, "sessions": []}, None
            self.loaded = True
            return True
        if code != 200:
            hint = {401: "token invalid or expired", 403: "token lacks access",
                    }.get(code, "unexpected response")
            print(f"[stats] couldn't load data (HTTP {code}: {hint}) -- skipping stats this run")
            return False
        try:
            raw = base64.b64decode(obj["content"]).decode()
            self.data = json.loads(raw) if raw.strip() else {}
        except Exception:
            print("[stats] data file unreadable -- skipping stats (not overwriting it)")
            return False
        self.data.setdefault("active", {})
        self.data.setdefault("sessions", [])
        self.sha = obj.get("sha")
        self.loaded = True
        return True

    def save(self):
        if not self.dirty:
            return True
        if not self.loaded:
            return False  # never overwrite data we failed to read
        payload = {
            "message": "stats update",
            "content": base64.b64encode(
                json.dumps(self.data, indent=1, sort_keys=True).encode()).decode(),
        }
        if self.sha:
            payload["sha"] = self.sha
        for attempt in range(1, 4):
            try:
                code, obj = self._call("PUT", payload)
            except Exception as e:
                code, obj = 0, {}
                print(f"[stats] save attempt {attempt}/3 failed ({type(e).__name__})")
            if code in (200, 201):
                self.sha = (obj.get("content") or {}).get("sha", self.sha)
                self.dirty = False
                return True
            if code:
                print(f"[stats] save attempt {attempt}/3 failed (HTTP {code})")
            if code in (401, 403, 404, 409, 422):
                break  # retrying the same request won't help
            time.sleep(3)
        print("[stats] couldn't save data this run")
        return False

    # ---- recording ----------------------------------------------------
    def observe(self, key, now, cls):
        """cls: 'public' | 'private' | 'off'."""
        act = self.data["active"].get(key)
        if cls in ("public", "private"):
            if act is None:
                act = {"obs": [], "offline_since": None}
                self.data["active"][key] = act
            if act.get("offline_since"):
                act["obs"].append([act["offline_since"], "o"])  # mark the gap
                act["offline_since"] = None
            act["obs"].append([iso(now), "p" if cls == "public" else "x"])
            self.dirty = True
            return
        if act is None:
            return
        if not act.get("offline_since"):
            act["offline_since"] = iso(now)
            self.dirty = True
        last_live = parse_iso(
            [o for o in act["obs"] if o[1] in ("p", "x")][-1][0])
        if (now - last_live).total_seconds() >= MERGE_GAP_S - MERGE_TOLERANCE_S:
            self._finalize(key, act)

    def _finalize(self, key, act):
        obs = [(parse_iso(t), c) for t, c in act["obs"]]
        res = compute_session(obs, parse_iso(act["offline_since"]))
        self.data["sessions"].append({
            "key": key,
            "start": iso(res["start"]),
            "end": iso(res["end"]),
            "public_s": round(res["public_s"]),
            "private_s": round(res["private_s"]),
            "notified": False,
        })
        del self.data["active"][key]
        self.dirty = True

    # ---- per-stream message -------------------------------------------
    def announce_pending(self, now, labels):
        if not self.webhook_url:
            return
        changed = False
        for s in self.data["sessions"][-20:]:
            if s.get("notified"):
                continue
            end = parse_iso(s["end"])
            if now - end > ENDED_RETRY_WINDOW:
                s["notified"] = True  # too old to be useful; stop retrying
                changed = True
                continue
            total = s["public_s"] + s["private_s"]
            fields = [{"name": "Streamed", "value": fmt_duration(total), "inline": True}]
            if s["private_s"] > 0:
                fields.append({"name": "Public", "value": fmt_duration(s["public_s"]), "inline": True})
                fields.append({"name": "Private", "value": fmt_duration(s["private_s"]), "inline": True})
            fields.append({"name": "Started",
                           "value": f"<t:{int(parse_iso(s['start']).timestamp())}:f>", "inline": False})
            fields.append({"name": "Ended",
                           "value": f"<t:{int(end.timestamp())}:f>", "inline": False})
            label = labels.get(s["key"])
            embed = {
                "title": "🔴 Stream ended" + (f" ({label})" if label else ""),
                "color": COLOR_ENDED,
                "fields": fields,
                "footer": {"text": "Durations are approximate (checks run about every 10 minutes)"},
            }
            ok = post_json(self.webhook_url, {
                "username": "Stripchat Stats", "embeds": [embed],
                "allowed_mentions": {"parse": []},
            })
            if ok:
                s["notified"] = True
                changed = True
        if changed:
            self.dirty = True

    # ---- weekly report ------------------------------------------------
    def _week_embeds(self, w_start, w_end, labels, preview):
        by_key = {}
        for s in self.data["sessions"]:
            st = parse_iso(s["start"])
            if w_start <= st < w_end:
                by_key.setdefault(s["key"], []).append(s)
        keys = sorted(by_key) or sorted(labels) or [None]
        last_day = (w_end - timedelta(seconds=1)).astimezone(TZ)
        period = f"{w_start.astimezone(TZ):%d %b} – {last_day:%d %b %Y}"
        embeds = []
        for key in keys:
            sess = by_key.get(key, [])
            label = labels.get(key) if len(labels) > 1 else None
            title = ("📊 Weekly report" + (" (week so far)" if preview else "")
                     + (f" — {label}" if label else ""))
            if not sess:
                embeds.append({"title": title, "description": f"{period}\n\nNo streams recorded this week.",
                               "color": COLOR_REPORT})
                continue
            durs = [(s["public_s"] + s["private_s"], s) for s in sess]
            total = sum(d for d, _ in durs)
            pub = sum(s["public_s"] for _, s in durs)
            priv = sum(s["private_s"] for _, s in durs)
            longest_d, longest_s = max(durs, key=lambda x: x[0])
            longest_day = parse_iso(longest_s["start"]).astimezone(TZ)
            # per-day breakdown (by local start day)
            day_tot, day_cnt = {}, {}
            for d, s in durs:
                day = parse_iso(s["start"]).astimezone(TZ).date()
                day_tot[day] = day_tot.get(day, 0) + d
                day_cnt[day] = day_cnt.get(day, 0) + 1
            first_day = w_start.astimezone(TZ).date()
            peak = max(day_tot.values()) or 1
            lines = []
            for i in range(7):
                day = first_day + timedelta(days=i)
                t = day_tot.get(day, 0)
                bar = "█" * max(1, round(10 * t / peak)) if t else ""
                lines.append(f"{day:%a %d}  {bar:<10}  {fmt_duration(t) if t else '—'}"
                             + (f" ({day_cnt[day]})" if t else ""))
            fields = [
                {"name": "Streams", "value": str(len(sess)), "inline": True},
                {"name": "Total time", "value": fmt_duration(total), "inline": True},
                {"name": "Average", "value": fmt_duration(total / len(sess)), "inline": True},
                {"name": "Public", "value": fmt_duration(pub) if pub else "—", "inline": True},
                {"name": "Private", "value": fmt_duration(priv) if priv else "—", "inline": True},
                {"name": "Longest",
                 "value": f"{fmt_duration(longest_d)} ({longest_day:%a})", "inline": True},
                {"name": "By day (streams in brackets)", "value": "```\n" + "\n".join(lines) + "\n```"},
            ]
            embeds.append({"title": title, "description": period, "color": COLOR_REPORT,
                           "fields": fields,
                           "footer": {"text": "A stream counts in the week it started. Times are approximate."}})
        return embeds

    def weekly_report(self, now, state, labels, force=False):
        """Returns True if `state` (the public state.json dict) was changed."""
        if not self.webhook_url:
            return False
        local = now.astimezone(TZ)
        this_monday = datetime(local.year, local.month, local.day, tzinfo=TZ) - timedelta(days=local.weekday())
        prev_monday = this_monday - timedelta(days=7)
        target_id = prev_monday.date().isoformat()

        if force:  # manual preview of the current week so far; touches no state
            embeds = self._week_embeds(this_monday, now + timedelta(seconds=1), labels, preview=True)
            ok = post_json(self.webhook_url, {"username": "Stripchat Stats", "embeds": embeds[:10],
                                              "allowed_mentions": {"parse": []}})
            print(f"[stats] forced preview report {'sent' if ok else 'FAILED'}")
            return False

        if STATE_REPORT_KEY not in state:
            state[STATE_REPORT_KEY] = target_id  # first deployment: no report for a week we never tracked
            return True
        if state[STATE_REPORT_KEY] == target_id:
            return False
        embeds = self._week_embeds(prev_monday, this_monday, labels, preview=False)
        ok = post_json(self.webhook_url, {"username": "Stripchat Stats", "embeds": embeds[:10],
                                          "allowed_mentions": {"parse": []}})
        if ok:
            state[STATE_REPORT_KEY] = target_id
            print("[stats] weekly report sent")
            return True
        print("[stats] weekly report failed -- will retry next run")
        return False

    # ---- orchestration ------------------------------------------------
    def run(self, observations, now, state, labels, force_report=False):
        """observations: list of (key, 'public'|'private'|'off').
        Returns True if the public `state` dict was modified."""
        if not self.load():
            return False
        for key, cls in observations:
            self.observe(key, now, cls)
        self.save()
        self.announce_pending(now, labels)
        self.save()
        state_changed = self.weekly_report(now, state, labels, force=force_report)
        if force_report:
            print(f"[stats] data repo OK ({len(self.data['sessions'])} finished session(s) stored)")
        return state_changed
