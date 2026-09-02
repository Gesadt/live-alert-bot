# Stripchat → Discord Live Alerts

Posts a message with a live thumbnail in your Discord server whenever a
Stripchat model goes live. Runs entirely on GitHub's free infrastructure —
nothing to host, nothing to pay for.

## How it works

A GitHub Actions workflow runs on a schedule (every 10 minutes). Each run:

1. Launches a real headless browser (Playwright/Chromium) and loads the
   model's public profile page (`stripchat.com/<username>`).
2. Reads the live/offline status directly out of the page's own embedded
   JSON state — the same data the page itself uses to render its LIVE
   badge — rather than calling Stripchat's internal API.
3. Reads the page's `og:image` meta tag for a current thumbnail.
4. Compares the live status to what it was last run (`state.json`,
   committed back to the repo). On an offline → live transition, it posts
   an embed to a Discord webhook with the thumbnail and a link.
5. Only marks state as "live" if the Discord post actually succeeds — a
   failed post gets retried on the next run instead of being silently lost.

### Why it's built this way (not a simpler version)

This design exists because of specific things that didn't work, found by
testing rather than assumption:

- **A plain scripted HTTP request (no browser) gets blocked.** Cloudflare
  blocklists cloud/datacenter IP ranges as a category — this affects any
  free serverless/CI platform equally (Google Apps Script, GitHub Actions,
  AWS Lambda, etc.), not just one of them.
- **A real headless browser is *not* blocked**, though — loading the plain
  profile page returns real content immediately (HTTP 200, no
  `cf-mitigated` header, correct page-specific title), even from a GitHub
  Actions IP. So Playwright is used, but not to "defeat" anything — the
  page was never actually blocked.
- **Stripchat's internal JSON status API is separately hardened**
  (`/api/front/v2/models/username/<u>/cam` returns HTTP 418, even from a
  real passed browser session) — this is deliberate and specific to that
  endpoint, so it's not used at all.
- **The live/offline flag is instead read from the page's own embedded
  state** (`viewCamBase.model.isLive` / `.status`, inside a `<script>`
  tag) — the same public data every visitor's browser already receives to
  render the page's own LIVE badge. Confirmed present and correctly
  readable in both live and offline states.
- **The thumbnail comes from the page's `og:image` meta tag**, not from
  Discord's own auto-link-preview. Relying on Discord to auto-embed a
  plain URL was tested and found unreliable (a documented Discord
  webhook quirk, worsened by Discord's own crawler potentially hitting
  the same bot-detection Stripchat applies elsewhere). Reading the image
  URL ourselves, using the same already-working browser fetch, sidesteps
  that entirely.
- **Discord webhook POSTs need a real `User-Agent` header.** Python's
  `urllib` default (`Python-urllib/3.x`) is blocklisted by Discord's edge
  and returns a bare `403 Forbidden` with no explanation — fixed by
  sending a normal browser-style User-Agent.

## Repo contents

| File | Purpose |
|---|---|
| `check_stripchat.py` | The production monitor — checks status, posts alerts. |
| `test_webhook.py` | Standalone script to test the Discord webhook in isolation. |
| `.github/workflows/monitor.yml` | Scheduled workflow (every 10 min) that runs the monitor. |
| `.github/workflows/test-webhook.yml` | Manual-trigger workflow to run the webhook test. |
| `state.json` | Auto-created/committed by the workflow — tracks last-known live status per model. Don't edit by hand while the workflow is active. |

## Setup

### 1. Create the Discord webhook
1. In your Discord server, go to the target channel → **Edit Channel** →
   **Integrations** → **Webhooks** → **New Webhook**.
2. Name it (e.g. "Stripchat Alerts") and optionally set an avatar.
3. Click **Copy Webhook URL** and keep it private — anyone with it can
   post to that channel with no login required.

### 2. Create a public GitHub repository
Public matters here specifically because Actions minutes are uncapped and
free only for public repos — a private repo running Chromium installs
every 10 minutes would exceed the free private-repo minute allowance.

Upload all the files in this project, preserving the `.github/workflows/`
folder structure.

### 3. Add the webhook URL as a secret
**Settings → Secrets and variables → Actions → New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Value: the webhook URL from step 1

### 4. Allow the workflow to commit state
**Settings → Actions → General → Workflow permissions** → select
**"Read and write permissions"** → Save.
(Needed so the workflow can commit `state.json` back to the repo.)

### 5. Configure the model(s) to watch — via a Secret, not the code
Don't put usernames directly in `check_stripchat.py`. Add another repo
secret so the username never appears in source code, commit history, or
Action logs (all of which are publicly browsable once the repo is public):

**Settings → Secrets and variables → Actions → New repository secret**
- Name: `STRIPCHAT_USERNAMES`
- Value: a comma-separated list, e.g. `JadeScarlet_LoveLace` or
  `model_one,model_two`

`check_stripchat.py` reads this at runtime via `os.environ`. Log output
uses a short one-way hash of each username instead of the plaintext, so
even Action run logs (also public on a public repo) don't leak it.

**If you're migrating an existing setup that had the username hardcoded:**
delete (or reset to `{}`) the currently-committed `state.json` before
making the repo public. The new code keys `state.json` by a hash instead
of the plaintext username, but swapping the code alone doesn't retroactively
scrub an already-committed plaintext key — it would just sit there
unchanged until manually removed. Since `state.json` auto-regenerates
(the "first run" behavior silently re-records current status without
alerting), it's safe to just delete it and let the next run recreate it.

### 6. (Optional) Configure the ping and embed color
In `.github/workflows/monitor.yml`:
```yaml
env:
  DISCORD_PING: "@here"   # or a role mention like "<@&ROLE_ID>", or "" for none
```
In `check_stripchat.py`:
```python
EMBED_COLOR = 0xFF3E7F  # decimal, not hex string
```

### 7. Test each piece independently
- **Webhook only:** Actions tab → **Test Discord Webhook** → **Run workflow**.
  A "✅ Test message" should appear in Discord.
- **Full monitor:** Actions tab → **Stripchat Live Monitor** → **Run workflow**.
  Check the log for a line like `<username>: isLive=False status='off'`
  (or `True`/`'public'` if currently live).

Once both work, the schedule takes over automatically — no separate
"enable" step is needed beyond the workflow file being committed to the
default branch.

## Behavior notes

- **First run is silent.** If a model is already live the very first time
  `state.json` doesn't exist yet, that's recorded without sending an
  alert — avoids pinging for a stream that may have already been running
  for a while before tracking started. Every transition after that alerts
  normally.
- **A failed Discord post doesn't lose the alert.** `send_alert()` retries
  up to 3 times; if it still fails, state is left as "not live" so the
  next scheduled run (≤10 min later) will detect the same transition and
  try again — no silent drops.
- **The thumbnail is dynamic, not a static profile photo.** The `og:image`
  URL encodes the model's fixed internal ID plus a snapshot timestamp that
  updates as Stripchat re-captures frames from the live feed, so each
  alert reflects a genuinely recent image, not a cached avatar.
- **GitHub auto-disables scheduled workflows after 60 days with zero
  commits to the repo.** In practice this shouldn't trigger, since
  `state.json` gets committed on every status change — but if alerts stop
  arriving, check the Actions tab for a disabled-workflow banner.
- **Cron timing isn't exact to the minute** — GitHub can delay scheduled
  runs slightly under platform load. Not significant for this use case.

## If it stops working

This reads Stripchat's own internal page structure and meta tags, which
aren't a published stable API — they can change this at any time without
notice. If the Actions log shows:
```
[warn] <username>: couldn't find embedded state (Stripchat may have changed their page structure)
```
that means the `viewCamBase.model` path moved or was renamed. The fix is
the same process used to find it originally: load the page, dump all
`<script>` tag contents, and search for the new field names (search for
`"isLive"` specifically — it's a fairly stable, low-level field name even
if the surrounding object structure changes).

If thumbnails stop appearing specifically (while `isLive` detection still
works), check whether the `og:image` meta tag is still present on the page
— that's independent of the internal JSON state and could change on its
own schedule.

## Version log

**v1 — Initial concept (not deployed).** Google Apps Script polling
Stripchat's internal JSON API directly. Blocked by Cloudflare's IP
reputation system (datacenter/cloud IP ranges are blocklisted as a
category) — confirmed this wasn't Apps-Script-specific by hitting the
same wall from GitHub Actions.

**v2 — Diagnostic phase.** Built a throwaway diagnostic using a real
headless browser (Playwright) to test whether executing Cloudflare's JS
challenge would get further than a plain HTTP request. Found: the plain
profile *page* isn't challenged at all (false alarm from an overly broad
keyword-based challenge-detector was corrected using Cloudflare's own
`cf-mitigated` response header instead of guessing from page text); the
internal JSON API *is* separately hardened (HTTP 418).

**v3 — Found the real data source.** Since the API was off-limits but the
page wasn't, located the live/offline flag embedded directly in the page's
own `<script>` content (`viewCamBase.model.isLive` / `.status`), using a
real JSON parser to locate it robustly rather than fragile regex.
Confirmed present and correct in both live and offline states.

**v4 — First production monitor.** GitHub Actions (scheduled every 10
min, public repo for free unlimited minutes) + Playwright + Discord
webhook, using the v3 data source. State persisted via a committed
`state.json` so alerts only fire on actual transitions.

**v5 — Fixed Discord webhook 403.** Root cause: Python's default
`urllib` User-Agent (`Python-urllib/3.x`) is blocklisted by Discord's
edge. Fixed by sending a normal browser User-Agent on webhook POSTs.

**v6 — Fixed GitHub Actions Node 20 deprecation warning.** Bumped
`actions/checkout` v4→v6 and `actions/setup-python` v5→v6, which target
Node 24 natively.

**v7 — Reliability hardening.** Added: retry logic (3 attempts) on the
webhook POST; state only updates to "live" on confirmed alert success (so
failures retry next run instead of being lost); first-run detection to
avoid alerting on a stream that was already live before tracking started.

**v8 — Reliable thumbnails.** Two earlier attempts to source a thumbnail
from Stripchat's internal JSON state (both a name-based key search and a
broader URL-shaped-value scan, run while the model was confirmed live)
came back empty — the app state apparently doesn't carry a usable image
URL within the searched structure. Switched to reading the standard
`og:image` meta tag instead (the same field designed for link
previews/SEO), which reliably returns a current, dynamically-updating
snapshot URL. This also replaced an earlier, unreliable approach that
depended on Discord's own auto-link-unfurl feature.

**v9 — Username privacy, for public-repo use.** Moved the watched
username(s) out of source code entirely into a `STRIPCHAT_USERNAMES`
secret (mirroring how the webhook URL was already handled), since source
code is the first thing visible on a public repo. Also switched
`state.json`'s keys from the plaintext username to a one-way hash, since
that file is a committed artifact that persists in git history — and
switched all console/log output to use the same hash label, since Action
run logs are also publicly browsable on a public repo. Net effect: the
actual username now only ever appears in-memory during a run and in the
Discord message itself, never in anything committed or logged.
