# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Use `python3`; `python` is not on PATH in this environment.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # fill in keys

uvicorn app:app --port 8000       # run the server

python3 -m unittest discover -s tests                                # all tests (stdlib, no keys)
python3 -m unittest tests.test_core.FlowTests.<test_name>            # single test (run from repo root)

# No meeting needed: replay a fake meeting against a DRY_RUN server
DRY_RUN=true RECALL_VERIFICATION_SECRET=whsec_dGVzdC1zZWNyZXQta2V5LTEyMzQ1Ng== uvicorn app:app --port 8000
RECALL_VERIFICATION_SECRET=whsec_dGVzdC1zZWNyZXQta2V5LTEyMzQ1Ng== python3 tools/simulate.py
```

There is no lint or build step configured.

## Architecture

The server receives a Google Meet transcript through a Recall.ai bot and has Claude act on it. Read these together to follow a request end to end:

- **Webhook entry** (`app.py` → `recall.verify_request`): `/webhooks/recall` checks the HMAC signature, then runs `copilot.handle_event` as a background task and acks immediately. Recall sends transcript webhooks serially, so handling must not block the endpoint.
- **Event routing** (`copilot.handle_event`): transcript lines go to the session, chat messages go to `handle_chat` (commands `ok N`, `no N`, `stats`, `focus on|off`, `agenda: …`), and lifecycle events (`bot.in_call_recording`, `bot.done`, …) start or stop the metrics clock and end the session.
- **Per-meeting loop** (`copilot._loop` → `copilot.tick`, every 1 s): two independent jobs.
  - *Detect*: every `DETECT_INTERVAL_S`, scans utterances after `detect_cursor` with `ACTION_MODEL`, forced tool `propose_actions`. Proposals are posted to chat, or run directly if `AUTO_EXECUTE`.
  - *Focus*: every `FOCUS_INTERVAL_S`, classifies the rolling `WINDOW_S` window with `FOCUS_MODEL`, forced tool `report_focus`. After `DRIFT_STRIKES` off-topic checks and past `WARN_COOLDOWN_S`, it posts a nudge. Skipped entirely when `mode == "daily"`.
  - *Daily timekeeping* (mode `daily` only): `Timekeeper` (`timekeeper.py`, pure logic) accumulates talk seconds per speaker from `transcript.data` word timestamps (`session.parse_transcript_duration`) and `tick` sends its due warnings. The per-person budget is `DAILY_TOTAL_S / N`. The attendee list comes from `POST /meetings` `participants`, else `agent.fetch_attendees` (Calendar MCP), else speakers seen.
  - *OpenProject* (daily only, when `OPENPROJECT_PROJECT_ID` is set): `agent.fetch_blocked` reads blocked HU at start (`openproject_mcp`, direct call via `mcp_direct.call_tool`) and they go into the detect prompt. `copilot._finish_daily` runs at `bot.done`: waits for Gemini notes (`meet_notes.wait_for_notes`, Drive), `agent.plan_unblock` builds a step-by-step plan, posts it as a comment per HU, and posts metrics to `OPENPROJECT_DAILY_WP_ID`. Only HU that were blocked at start get comments.
- **Proposal lifecycle** (`session.Proposal.status`): `pending → approved → done | failed`, or `rejected`. `copilot.run_proposal` executes via `agent.execute_action`, which calls the MCP servers through the Anthropic MCP connector beta. An action counts as `done` only if at least one MCP tool call ran and none returned an error.
- **Claude calls** (`agent.py`): all three calls live here: detect, execute, focus. Only `execute_action` has side effects.
- **Metrics** (`metrics.py`): on-topic time is a share of total meeting time. Each focus check claims only the last `WINDOW_S` seconds, and uncovered time is "unclassified", so the buckets sum to 100%. MCP executions are tracked per call. Finished meetings are appended to `metrics/meetings.jsonl`.
- **Config** (`config.py`): every tunable comes from an env var. `load_mcp_servers` expands `${VAR}` placeholders and drops servers whose token is unset. The `description` field is local-only and is stripped before the API call (`agent._api_server`).

## Gotchas

- Session state (`copilot.SESSIONS`, `_LOOPS`) is in memory. Run a single worker; a restart loses live meetings.
- Every bot chat message starts with `CHAT_PREFIX` (🤖). Incoming chat events with that prefix are ignored so the bot does not react to its own messages.
- MCP server URLs must be public HTTPS. Claude calls them from Anthropic's cloud, so `localhost` will not work.
- The Recall auth header is the bare API key, with no `Token ` prefix. Set `RECALL_API_KEY="Token <key>"` only if an older workspace returns 401.
- Tests stub both the Claude and Recall calls, so they run without keys. Changes to `agent.py` prompts or tool schemas are not covered by tests and need a `DRY_RUN` simulation to verify.
