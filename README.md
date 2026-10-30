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
| `mcp_servers.json` | Your MCP servers; `description` tells the detector what each one can do |
| `tools/simulate.py` | Replays a fake meeting against your local server (no Meet needed) |
| `tests/test_core.py` | Tests for signatures, parsing and the propose → approve → execute and focus flows |

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

### 5. Send the bot to a meeting

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

Set `AUTO_EXECUTE=true` to skip approvals. This isn't recommended at first, because speech-to-text plus live conversation produces false positives.

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

## Before production

- **State is in memory.** Move `SESSIONS` to Redis or Postgres if you run more than one worker or restart mid-meeting. A restart also loses the live metrics of meetings in progress; finished meetings are already in `meetings.jsonl`.
- **Schedule bots in advance.** Last-minute bot creation is more likely to hit `507` errors, which `create_bot` retries. Recall's Calendar integration can auto-join every meeting on a user's Google Calendar.
- **Consent.** The bot pins a notice in the chat when it joins. If you record people in Colombia, Ley 1581 (habeas data) applies, so define how long transcripts are kept and who can see them.
- **Chat limits.** Meet chat messages are capped at 500 characters and go to everyone, so a nudge is visible to all participants. For private nudges to the host, send them through your Slack MCP server instead.
- **Recall auth header.** This code sends the bare API key, as Recall's current docs show. If you get 401s on an older workspace, set `RECALL_API_KEY="Token <key>"`.
