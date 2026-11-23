# Meet Copilot

A Recall.ai bot joins your Google Meet. Its live transcript streams to this server. Claude then:

1. **Detects actions** in what people say ("Luis, open a ticket for the login crash, due Friday"). It proposes them in the Meet chat and runs them through your MCP servers once someone replies `ok <n>`.
2. **Watches focus**: every ~45 s it compares the last ~2.5 minutes against the agenda. After two off-topic checks in a row it posts a gentle nudge in the chat, then stays quiet for a 3-minute cooldown.

```
Google Meet ──► Recall bot ──transcript.data / chat webhooks──► /webhooks/recall (verify HMAC, ack fast)
                    ▲                                                   │
                    │ send_chat_message                                 ▼
                    │                                     per-meeting loop (copilot.py)
                    │                        ┌───────────────┴────────────────┐
                    │              every 15s: detect_actions      every 45s: check_focus
                    │              (Sonnet, forced tool → JSON)   (Haiku, forced tool → JSON)
                    │                        │                                │
                    └──── "Proposal #1 … reply ok 1" ◄──┘       2 strikes + cooldown → "⏱️ Focus check…"
                                             │ "ok 1" in chat
                                             ▼
                          execute_action: Claude + MCP connector (jira / slack / calendar …)
                                             │
                                             └──► "✅ #1: Created PROJ-42"
```

## Files

| File | What it does |
|---|---|
| `app.py` | FastAPI routes: start a meeting, inspect its state, receive Recall webhooks |
| `copilot.py` | Event routing, chat commands, per-meeting detect/focus loop |
| `agent.py` | The three Claude calls: detect, execute (MCP), focus check |
| `recall.py` | Create bot, send chat, leave, verify Recall signatures |
| `session.py` | Meeting state and parsers for Recall payloads and chat commands |
| `metrics.py` | On-topic time share and MCP execution tracking; JSON logs and `metrics/meetings.jsonl` |
| `timekeeper.py` | Daily mode: per-person talk-time budget and overrun warnings (pure logic, no I/O) |
| `report.py` | Daily mode: end-of-meeting report saved to `METRICS_DIR/daily/<bot_id>.json` |
| `calendar_sync.py` | Google Calendar OAuth connection: hands the refresh token to Recall and keeps the calendar id (not yet used for attendees) |
| `notify.py` | Optional Slack / Google Chat webhook that announces a meeting before the bot joins |
| `static/timer.html` | Timer page the bot shows as its camera in daily mode |
| `mcp_servers.json` | Your MCP servers; `description` tells the detector what each one can do |
| `tools/simulate.py` | Replays a fake meeting against your local server (no Meet needed); `--mode daily` too |
| `stubs/` | Local Calendar MCP stub (fake events, bearer token) for controlled tests |
| `gcal_mcp/` | Google Calendar MCP server (real Calendar API, bearer token). Optional `gcal` compose profile |
| `Dockerfile`, `docker-compose.yml` | Container image, app service, optional `gcal` and `stub` profiles, named volumes |
| `tests/test_core.py`, `tests/test_calendar_stub.py` | Tests for signatures, parsing, the propose → approve → execute and focus flows, and the calendar stub |

## Setup

### 1. Recall.ai dashboard (one-time)

- **API key**: Developers → API Keys. Note your region (`us-west-2` for pay-as-you-go).
- **Workspace verification secret**: Developers → API Keys & Secrets → *Create Workspace Secret* (`whsec_…`). Recall only sends signature headers once this exists.
- **Webhook endpoint**: point it at `https://<PUBLIC_URL>/webhooks/recall` and subscribe to `bot.in_call_recording`, `bot.done` and `transcript.failed`. These tell the server when the meeting clock starts and stops, which the metrics depend on.

### 2. A stable public URL

Recall has to reach your server. In development, use a **static** ngrok domain:

```bash
ngrok http --url=your-static-domain.ngrok-free.app 8000
```

### 3. MCP servers

Edit `mcp_servers.json`. Each entry needs a publicly reachable HTTPS MCP URL, because Claude calls it from Anthropic's cloud and `localhost` won't work. Tokens go in `.env` and are referenced as `${VAR}`. Servers whose token is unset are skipped automatically.

### 4. Install and run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in keys
uvicorn app:app --port 8000
```

### Or with Docker

Docker is the recommended way to deploy. The image is `python:3.12-slim`, runs as a non-root user (uid 10001), and has a healthcheck on `/metrics`.

```bash
cp .env.example .env                  # fill in keys first (see "Deploy with Docker" below)
docker compose up -d --build          # serves on http://localhost:${PORT:-8000}
docker compose ps                     # STATUS should read "healthy"
docker compose logs -f app            # chat messages in DRY_RUN show up here
docker compose run --rm --no-deps app python -m unittest discover -s tests
docker compose down                   # keeps the volumes; add -v only to delete data
```

## Deploy with Docker

### What the stack contains

| Piece | Where | Persisted in |
|---|---|---|
| `app` (FastAPI + copilot loop) | `Dockerfile`, service `app` | image |
| Meeting metrics, `meetings.jsonl`, daily reports | `/app/metrics` | named volume `metrics` |
| Google calendar ids from OAuth (`CALENDAR_DB`, not yet used) | `/app/data/calendars.json` | named volume `data` |
| Calendar MCP stub (optional, profile `stub`) | `stubs/`, service `calendar-stub` | named volume `stub-data` |

Compose passes all of `.env` to the `app` container (`env_file`). Two values are forced inside the container: `METRICS_DIR=/app/metrics` and `CALENDAR_DB=/app/data/calendars.json`. `DRY_RUN` comes from your shell and defaults to `false`. `.env` is excluded from the image by `.dockerignore`, so keys are never baked in.

### Steps on a server

1. **Install** Docker Engine with the Compose plugin (`docker compose version` must work).
2. **Clone** the repo and copy the env file:
   ```bash
   git clone <repo-url> && cd assistant-AI-agent-for-a-daily-stand-up
   cp .env.example .env && chmod 600 .env
   ```
3. **Fill `.env`**. Required for a real meeting:
   - `RECALL_API_KEY`, `RECALL_VERIFICATION_SECRET`, `RECALL_REGION`
   - `PUBLIC_URL`: the public HTTPS address of this server (for example `https://copilot.example.com`). Recall, the timer page and the Google redirect all use it
   - `ANTHROPIC_API_KEY`
   - MCP tokens for the servers you use (`JIRA_MCP_TOKEN`, `SLACK_MCP_TOKEN`, `CALENDAR_MCP_TOKEN` with `CALENDAR_MCP_URL`). Servers with no token are skipped
   - Optional: `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` (calendar connection), `NOTIFY_WEBHOOK_URL` (meeting notice)
4. **Put HTTPS in front.** The app listens on plain HTTP on port `PORT` (default 8000). Use a reverse proxy (Caddy, nginx, Traefik) or a tunnel with a TLS certificate, and point `PUBLIC_URL` at it. Recall, the bot's camera page and Google OAuth all need that public URL. Do not expose port 8000 directly without TLS.
5. **Start and check**:
   ```bash
   docker compose up -d --build
   curl -s http://127.0.0.1:8000/metrics          # JSON totals, even with zero meetings
   curl -s https://<your-public-host>/metrics      # same, through the proxy
   ```
6. **Register the webhook** in Recall (Developers → Webhooks) at `https://<PUBLIC_URL>/webhooks/recall`, subscribed to `bot.in_call_recording`, `bot.done` and `transcript.failed`. See Setup step 1.
7. **Update** later with `git pull && docker compose up -d --build`. The volumes stay.

### Single worker only

Live meetings are kept in memory. Run one container with one uvicorn worker (the image's default `CMD`). Restarting the container loses meetings in progress; finished meetings are already in the `metrics` volume. Plan restarts outside meeting hours.

### Volumes and backups

- `metrics` holds `meetings.jsonl` and `daily/<bot_id>.json`. These contain per-person talk time, which is personal data. Back them up encrypted and apply the retention rules in "Before production".
- `data` holds only Recall calendar ids from the OAuth connection. Keep it so reconnecting is not needed once that id is used.
- Backup from the host, for example (`docker volume ls` shows the exact volume name, prefixed with the project folder name):
  ```bash
  docker run --rm -v <project>_metrics:/src -v "$PWD":/dst alpine \
    tar czf /dst/metrics-$(date +%F).tgz -C /src .
  ```

### Test Docker without keys

```bash
DRY_RUN=true RECALL_VERIFICATION_SECRET=whsec_dGVzdC1zZWNyZXQta2V5LTEyMzQ1Ng== docker compose up -d --build
docker compose logs -f app                     # chat messages print here
RECALL_VERIFICATION_SECRET=whsec_dGVzdC1zZWNyZXQta2V5LTEyMzQ1Ng== python3 tools/simulate.py --mode daily
```

With `DRY_RUN=true`, Recall is never called and the bot does not join anything. `ANTHROPIC_API_KEY` is still needed if you want detections and focus checks in the replay.

### Full Docker test with the calendar stub

```bash
CALENDAR_MCP_TOKEN=stub-local-token docker compose --profile stub up -d --build calendar-stub
# then expose port 9000 over HTTPS (ngrok or cloudflared) and set in .env:
#   CALENDAR_MCP_URL=https://<your-tunnel>/mcp
#   CALENDAR_MCP_TOKEN=stub-local-token
docker compose up -d                             # restart the app to read the new values
```

`docker compose down` stops both services; `stub-data` keeps the stub's `calls.jsonl`.

### Controlled test with a local Calendar stub

Test-only. Not needed for the Google connection or for production. `docker compose up` does not start it (profile `stub`).

`stubs/calendar_mcp.py` is a fake Calendar MCP server. It serves the fixed events in `stubs/calendar_fixture.json`, requires the bearer token, and records every call to `stubs/calls.jsonl` (or `/data/calls.jsonl` in its container). `create_event` creates nothing.

```bash
# 1. Stub on its own (container)
CALENDAR_MCP_TOKEN=stub-local-token docker compose --profile stub up -d --build calendar-stub

# 2. Its tests (needs stubs/requirements.txt installed)
python3 -m unittest tests.test_calendar_stub

# 3. Anthropic cannot reach localhost: expose the stub over HTTPS (ngrok or cloudflared)
#    and point the app at it. Then restart the app so it reads the new variables.
CALENDAR_MCP_URL=https://<your-tunnel>/mcp CALENDAR_MCP_TOKEN=stub-local-token docker compose up -d
```

With `ANTHROPIC_API_KEY` set, a meeting created with `meeting_url` and no `participants` asks the stub for attendees. `GET /meetings/<bot_id>` should then list `Ana`, `Luis` and `Carla` under `timekeeper.expected`. Without `CALENDAR_MCP_URL`, the calendar entry in `mcp_servers.json` is dropped and the app uses speakers seen instead.

### 5. Google Calendar and meeting notices (optional)

Attendees for daily meetings come from a **Google Calendar MCP server**. This repo ships one in `gcal_mcp/`. It calls the Google Calendar API with your account's refresh token, behind a bearer token. The app reaches it through the `calendar` entry in `mcp_servers.json`, which needs `CALENDAR_MCP_URL` and `CALENDAR_MCP_TOKEN`. The OAuth connection below (`/calendar/google/connect`) is separate: it stores a Recall calendar id that nothing reads yet.

**Steps**

1. In Google Cloud, enable the **Google Calendar API** for the same project as the OAuth client from step 1 (`GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`).
2. Get a **refresh token** for the account whose calendar holds the stand-up. Use [Google OAuth Playground](https://developers.google.com/oauthplayground) with "Use your own OAuth credentials" set to your client, scope `https://www.googleapis.com/auth/calendar.events`, access type *offline*. Keep the refresh token in `.env` as `GOOGLE_REFRESH_TOKEN`.
   - If the OAuth consent screen is in *Testing* status, Google expires refresh tokens after 7 days. Publish the app or repeat this step when that happens.
3. Set in `.env`:
   ```bash
   GOOGLE_REFRESH_TOKEN=1//…
   GCAL_CALENDAR_ID=primary                         # or the calendar's id
   CALENDAR_MCP_TOKEN=<output of: openssl rand -hex 32>   # bearer token the app sends; the server checks it
   CALENDAR_MCP_URL=https://<public-host>/mcp       # see step 4
   ```
4. Start the server and give it public HTTPS. Anthropic calls it from the cloud, so `localhost` won't work:
   ```bash
   docker compose --profile gcal up -d --build gcal-mcp     # listens on :9100
   ```
   Put a reverse proxy or tunnel (Caddy, ngrok, cloudflared) in front of port 9100 and route `https://<public-host>/mcp` to it.
5. Check it: without the token the server must answer 401.
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST https://<public-host>/mcp     # expect 401
   ```
   Then start a daily meeting **without** `participants`. `GET /meetings/<bot_id>` should list the event's invitees under `timekeeper.expected`.

The server exposes two tools: `get_event_attendees(meeting_url)` returns invitee names of the event whose Meet link matches (searches 24 h either side of now), and `create_event(summary, start, end, attendees)` creates an event and sends invitations (used only after someone approves it in chat). It uses the account's primary calendar unless `GCAL_CALENDAR_ID` says otherwise. Anyone with the refresh token and the bearer token can read and create events in that calendar, so keep both secret.

The `stub` profile is for tests only. It is not needed for Google.

1. **Google Cloud Console** → APIs & Services → enable the Google Calendar API. Create an OAuth client of type **Web application**.
2. Add the **authorized redirect URI** `https://<PUBLIC_URL>/calendar/google/callback`.
3. Put the client id and secret in `.env` as `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`.
4. Open `https://<PUBLIC_URL>/calendar/google/connect` once in a browser and sign in with the Google account whose calendar holds the stand-up. Each account needs its own connection. The callback returns `{"ok": true, "calendar_id": "…"}`.
5. The calendar id is saved to `CALENDAR_DB` (`data/calendars.json` locally, `/app/data/calendars.json` in Docker). It is kept for future use; it does not feed the attendee lookup yet. Attendees come from the Google Calendar MCP server above.

The OAuth state expires after 10 minutes. If the callback fails, start again from `/connect`.

**Notices:** set `NOTIFY_WEBHOOK_URL` to a Slack or Google Chat incoming webhook. When a meeting is created, the app posts the title and link there, so the team can join and the bot waits in the lobby to be admitted. The notice never blocks the meeting: if the post fails, the meeting still starts.

### 6. Send the bot to a meeting

```bash
curl -X POST localhost:8000/meetings -H 'content-type: application/json' -d '{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "agenda": "1) Q4 mobile release date 2) Pricing change 3) Support hiring"
}'
# → {"bot_id": "…"}   then admit the bot from the Meet waiting room
curl localhost:8000/meetings/<bot_id>     # live state: last lines, proposals, current topic
```

## Chat commands (typed in the Meet chat)

| Command | Effect |
|---|---|
| `ok 3` / `sí 3` | Approve proposal #3 and execute it |
| `no 3` | Skip proposal #3 |
| `focus off` / `focus on` | Mute or unmute focus nudges |
| `stats` | Post the current metrics in the chat |
| `agenda: budget, roadmap` | Replace the agenda mid-meeting |
| `timer` / `tiempo` | Post each person's talk time against their budget (daily mode) |
| `participants: Ana, Luis, Carla` / `participantes: …` | Replace the expected attendee list and recompute budgets (daily mode) |

Set `AUTO_EXECUTE=true` to skip approvals. This isn't recommended at first, because speech-to-text plus live conversation produces false positives.

## Daily mode

Send `"mode": "daily"` when you create the meeting. For a stand-up this turns off focus nudges and splits a talk-time budget between the attendees:

```bash
curl -X POST localhost:8000/meetings -H 'content-type: application/json' -d '{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "agenda": "Daily: yesterday, today, blockers",
  "mode": "daily",
  "participants": ["Ana", "Luis", "Carla"]
}'
```

- **Who counts as attendee**, in this order: the `participants` list; otherwise the calendar event that holds the Meet link, if you configured a `calendar` MCP server; otherwise the people who have spoken so far. The budget uses the larger of the expected list and the speakers seen.
- **Budget**: `DAILY_TOTAL_S / N`, never below `DAILY_MIN_TURN_S`. Time is measured from speech, using Recall's word timestamps when present and about 2.5 words per second otherwise.
- **Warnings** go to the chat, once each: at 100 % of a person's turn (soft), at 150 % (firm, suggests the parking lot), and at 80 % and 100 % of the daily total.
- `GET /meetings/<bot_id>` returns the per-person breakdown under `timekeeper`. It is not written to `metrics/meetings.jsonl`, because talk time per person is personal data.
- **Timer page:** when the bot joins, it shows `/meetings/<bot_id>/timer` as its camera (Recall output media), so everyone sees the live budgets. `GET /meetings/<bot_id>/timer.json` returns the same data as JSON.
- **Projects:** every `PROJECT_INTERVAL_S` seconds Claude labels the project under discussion (`📌 Proyecto en discusión: …` in the chat). The labels appear in the timer page and the report.
- **Report:** `GET /meetings/<bot_id>/report` returns talk time, projects and proposal outcomes, live during the meeting and from `METRICS_DIR/daily/<bot_id>.json` afterwards. It is written automatically when the meeting ends. Keep it private.

## Metrics

Two metrics are tracked for every meeting.

**1. In-topic time, as a percentage of total meeting time.** The meeting clock runs from when the bot starts recording (`bot.in_call_recording`, or the first transcript line as a fallback) until the call ends. Each focus check assigns the time since the previous check to the status Claude returned. A check only vouches for the last `WINDOW_S` seconds (150 by default), so a long gap isn't all credited to one verdict. Time no check covered is *unclassified*: silence, periods with `focus off`, and the tail after the last check. The four buckets always add up to 100%:

| Field | Meaning |
|---|---|
| `in_topic_pct` | On-topic seconds ÷ total meeting seconds. This is the headline number |
| `off_topic_pct`, `drifting_pct` | The same calculation for the other two statuses |
| `unclassified_pct` | Time no focus check covered |
| `in_topic_pct_of_classified` | On-topic ÷ classified time only. Useful when the meeting had long silences |

**2. MCP executions.** Each approved action that runs is logged with the MCP tool calls Claude made, read from the `mcp_tool_use` and `mcp_tool_result` blocks in the response. An execution counts as `ok` only if at least one tool ran and none returned an error. A run where Claude replied without calling any tool counts as failed.

| Field | Meaning |
|---|---|
| `executions`, `executions_ok`, `executions_failed` | Approved actions that were run, and how they ended |
| `tool_calls`, `tool_calls_failed`, `tool_calls_by_server` | Individual MCP tool calls. One action can make several |
| `log` | Per execution: proposal, server, tools called, duration, error |

**Where to read them**

- `GET /meetings/<bot_id>/metrics`: live numbers during the meeting, or the stored summary afterwards.
- `GET /metrics`: totals across all meetings. In-topic % is weighted by time, so long meetings count more.
- `stats` typed in the Meet chat posts a one-line summary.
- Logs: every focus check, MCP execution and meeting end is written as one JSON line on the `metrics` logger, e.g. `{"metric_event": "mcp_execution", "ok": true, "tool_calls": [...]}`. You can ship these to Datadog, CloudWatch or BigQuery as they are.
- `metrics/meetings.jsonl` (set the folder with `METRICS_DIR`) gets one line per finished meeting.

Example summary:

```json
{
  "bot_id": "…", "status": "ended", "meeting_seconds": 2400.0,
  "focus": { "in_topic_pct": 71.3, "drifting_pct": 8.1, "off_topic_pct": 6.9, "unclassified_pct": 13.7,
             "in_topic_pct_of_classified": 82.6, "checks": 46 },
  "mcp":   { "executions": 4, "executions_ok": 3, "executions_failed": 1,
             "tool_calls": 6, "tool_calls_failed": 1, "tool_calls_by_server": {"jira": 4, "calendar": 2} }
}
```

## Try it without a meeting

```bash
DRY_RUN=true RECALL_VERIFICATION_SECRET=whsec_dGVzdC1zZWNyZXQta2V5LTEyMzQ1Ng== uvicorn app:app --port 8000
# in another terminal (same secret):
RECALL_VERIFICATION_SECRET=whsec_dGVzdC1zZWNyZXQta2V5LTEyMzQ1Ng== python tools/simulate.py
```

The server prints every chat message it would have sent. You need a real `ANTHROPIC_API_KEY`, and action proposals only appear if at least one MCP server in `mcp_servers.json` has its token set.

Tests (stdlib only, no keys needed): `python -m unittest discover -s tests`

## Tuning

| Variable | Default | Notes |
|---|---|---|
| `TRANSCRIPT_LANGUAGE` | `en` | Use `es` for Spanish meetings |
| `DETECT_INTERVAL_S` | 15 | Lower means faster proposals and more API calls |
| `FOCUS_INTERVAL_S` / `WINDOW_S` | 45 / 150 | How often and over how much talk focus is judged |
| `DRIFT_STRIKES` / `WARN_COOLDOWN_S` | 2 / 180 | Raise these if the nudges feel naggy |
| `DAILY_TOTAL_S` / `DAILY_MIN_TURN_S` | 900 / 30 | Daily mode: talk-time budget for the whole stand-up, and the per-person floor |
| `PROJECT_INTERVAL_S` | 60 | Daily mode: how often the project in discussion is re-labeled |
| `MIN_WINDOW_WORDS` | 40 | Focus checks are skipped when the window has fewer words (silence) |
| `CALENDAR_DB` | `data/calendars.json` | Where calendar ids are stored. Docker uses `/app/data/calendars.json` |
| `NOTIFY_WEBHOOK_URL` | empty | Slack or Google Chat webhook for meeting notices. Empty = off |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | empty | Needed only for `/calendar/google/connect` |

## Before production

- **State is in memory.** Move `SESSIONS` to Redis or Postgres if you run more than one worker or restart mid-meeting. A restart also loses the live metrics of meetings in progress; finished meetings are already in `meetings.jsonl`.
- **Persistent volumes.** Keep the `metrics` and `data` volumes on disk that is backed up. Losing `data` only means reconnecting Google; attendee lookup does not use it yet.
- **Secrets.** Keep `.env` out of git (already in `.gitignore`) and out of the image (`.dockerignore`). Rotate any key that was ever pasted into a chat or a commit.
- **Schedule bots in advance.** Last-minute bot creation is more likely to hit `507` errors, which `create_bot` retries. Recall's Calendar integration can auto-join every meeting on a user's Google Calendar.
- **Consent.** The bot pins a notice in the chat when it joins. If you record people in Colombia, Ley 1581 (habeas data) applies, so define how long transcripts are kept and who can see them.
- **Chat limits.** Meet chat messages are capped at 500 characters and go to everyone, so a nudge is visible to all participants. For private nudges to the host, send them through your Slack MCP server instead.
- **Recall auth header.** This code sends the bare API key, as Recall's current docs show. If you get 401s on an older workspace, set `RECALL_API_KEY="Token <key>"`.
