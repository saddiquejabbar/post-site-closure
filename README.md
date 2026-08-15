# Post-Site Closure Agent

An installer-first operations workflow that turns a Daily Meeting Report (DMR) into timely Telegram check-ins and consistent CRM-ready notes.

The technician never needs to open a second slow application after a visit. They reply in the Telegram group they already use. The agent asks only for information that is genuinely missing, keeps their wording close to verbatim, and produces a visible `CRM NOTE — DRAFT ONLY` for operations review.

## Why teams adopt it

- **Works where technicians already communicate.** Site updates stay in Telegram instead of forcing a separate Zoho CRM login after every visit.
- **One approval, clear visibility.** Operations sees every parsed site, time, location, and assigned technician before anything is scheduled.
- **Fewer questions.** A completed installation can close with `YES`; servicing asks for the actual finding and action; follow-ups appear only when the CRM record would otherwise be incomplete.
- **No 20-second polling loop.** Approved visits become exact, durable, one-shot OpenClaw jobs. The process sleeps between events and each job deletes itself after delivery.
- **Safe by default.** A DMR is only a draft until an authorized person presses **Approve Schedule**. CRM output remains draft-only unless an integration is deliberately enabled later.
- **Auditable rules.** Parsing, approval, scheduling, question selection, and state transitions are deterministic and covered by tests.

## Daily operating flow

```text
run post-site
      ↓
paste Daily Meeting Report and tag the bot
      ↓
Telegram preview: site · time · location · technician
      ↓
[✅ Approve Schedule] [✏️ Make Changes] [❌ Cancel]
      ↓
exact one-shot check-in at each approved DMR time
      ↓
technician replies naturally in Telegram
      ↓
only genuinely missing details are requested
      ↓
CRM NOTE — DRAFT ONLY, visible to operations
```

Example DMR:

```text
📋 Daily Meeting Report — 2026-08-16 (Sun)

1. Customer Alpha - 10:30am
📍 Central District | Servicing
Gateway offline

2+6. Customer Beta - 2pm
📍 North District | Installation
4x switches
1x smart hub
```

Telegram preview:

```text
POST-SITE SCHEDULE — 16 AUG

1. 10:30am — Customer Alpha @ Central District — Staff 1
2. 2pm — Customer Beta @ North District — Staff 2 + Staff 6

Nothing is scheduled until you approve.
```

## Question policy

The DMR already describes the planned work, so the agent does not repeat a long checklist.

- **Installation:** “Was everything installed/completed as planned?” A `YES` closes it. An incomplete answer gets one compact request for the outstanding work and next action.
- **Servicing:** Captures what was found and what was done. A bare `YES` gets one short request for the resolution detail because `YES` alone is not useful in CRM.
- **Handover:** Confirms handover and that the system is working.
- **Delivery:** Confirms all items and who received them.
- **Site meeting or assessment:** Captures the finding or agreement and next action.
- **Testing:** Captures the test result and any required follow-up.
- **Payment:** Asked only when the DMR explicitly says `collect payment`.

Technician wording is preserved instead of being rewritten into unsupported conclusions.

## Architecture

The recommended deployment uses one Telegram connection: OpenClaw owns Telegram, and the included extension owns deterministic DMR review and scheduling.

```text
Telegram group
  └─ OpenClaw gateway (single bot connection)
      ├─ post-site-closure-inbound extension
      │   ├─ DMR review and approval buttons
      │   └─ exact one-shot OpenClaw jobs
      ├─ Python workflow
      │   ├─ deterministic parser and question rules
      │   └─ SQLite draft/site state
      └─ post-site OpenClaw skill
          └─ natural technician replies and CRM-note draft
```

### How polling was removed

The first scheduler checked for due work every few seconds. This release replaces that hot loop with two low-wake mechanisms:

1. In the recommended OpenClaw deployment, approval creates one exact `at` job per site. There is no application polling loop.
2. The optional standalone Python runner calculates the next due appointment and sleeps until that instant. Ingesting a new DMR wakes it immediately; an idle schedule sleeps indefinitely.

This reduces needless database reads, CPU wake-ups, log noise, and timing drift while keeping site check-ins durable across restarts.

## Safety and reliability

- Two-phase approval: claim the draft, create all jobs, then commit the exact job IDs.
- Rollback: if any job cannot be created, created jobs are removed and the draft returns to pending.
- Idempotent declaration keys prevent duplicate check-ins.
- Group and sender IDs authorize DMR ingestion and approval.
- SQLite stores drafts and workflow state locally.
- Telegram button cleanup is best-effort after a durable commit, so a harmless message-edit error cannot falsely report that scheduling failed.
- CRM/webhook delivery is opt-in and disabled for the draft-only operating mode.

## Requirements

- OpenClaw 2026.7.1 or newer
- A Telegram bot/account connected to the OpenClaw gateway
- Python 3.11 or newer
- Node.js 20 or newer for extension tests
- A dedicated Telegram operations group and a dedicated OpenClaw `post-site` agent are strongly recommended

No LLM is used to decide whether a schedule is approved or when a message is sent. OpenClaw may handle natural-language technician replies, while deterministic Python owns the operational rules and state.

## Installation

### 1. Clone and test the Python workflow

```bash
git clone https://github.com/saddiquejabbar/post-site-closure.git
cd post-site-closure
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python -m pytest -q
```

Set the local staff names and Telegram mentions in `.env`. Never commit that file.

### 2. Install the OpenClaw extension

Link or copy `openclaw-extension/` to:

```text
~/.openclaw/extensions/post-site-closure-inbound/
```

The extension imports OpenClaw's plugin SDK. If OpenClaw is installed globally and Node cannot resolve it from the extension directory, link the installed package:

```bash
mkdir -p openclaw-extension/node_modules
ln -s "$(npm root -g)/openclaw" openclaw-extension/node_modules/openclaw
```

### 3. Install the OpenClaw skill

Copy `openclaw-skill/post-site-closure-dmr/` into the dedicated agent workspace:

```text
~/.openclaw/workspace-post-site/skills/post-site-closure-dmr/
```

### 4. Configure OpenClaw

Merge [the example configuration](docs/openclaw-config.example.json) into your existing `~/.openclaw/openclaw.json`. Replace every placeholder and preserve existing agents, bindings, plugins, and allow-list entries.

Important configuration boundaries:

- Bind only the intended Telegram group to the `post-site` agent.
- Use Telegram's numeric group and sender IDs, not display names.
- Only authorized sender IDs can ingest or approve a schedule.
- Use absolute paths for Python, the project, database, and OpenClaw executable.
- Do not run `telegram_bot.py` with the same bot token as OpenClaw; OpenClaw should remain the single Telegram connection in the recommended deployment.

Restart the OpenClaw gateway after configuration and confirm that `post-site-closure-inbound` is loaded.

## Daily use

In the configured Telegram group:

1. Send `run post-site`.
2. Paste the day's DMR.
3. Reply to the DMR and tag the bot if privacy mode means it did not see the paste directly.
4. Review the site/time/technician preview.
5. Press **Approve Schedule**, **Make Changes**, or **Cancel**.
6. Technicians reply directly to the timed check-in messages.
7. Review the resulting `CRM NOTE — DRAFT ONLY` in Telegram.

Nothing is scheduled before approval. **Make Changes** invalidates the current draft and waits for a corrected DMR. A newer draft replaces an older unapproved draft in the same group.

## Local development

Parse a sanitized sample DMR:

```bash
python cli.py ingest examples/dmr_demo.md
python cli.py prompts --date 2026-08-12
python cli.py list
```

Run all deterministic tests:

```bash
python -m pytest -q
cd openclaw-extension && npm test
```

Optional HTTP endpoints for an n8n or internal integration:

```bash
uvicorn api:app --reload --port 8080
```

- `GET /health`
- `POST /dmr`
- `GET /sites`
- `GET /sites/{id}/prompt`
- `POST /sites/{id}/reply`
- `GET /sites/{id}/crm-payload`

## CRM integration boundary

The default operating mode never writes to Zoho or another CRM. It produces a reviewed draft in Telegram first.

If a later rollout enables CRM delivery, point `CRM_WEBHOOK_URL` to a controlled n8n endpoint and map fields there. Do not hard-code guessed CRM field IDs in this project. Recommended rollout:

```text
Telegram draft-only pilot
→ measure technician response and note quality
→ supervisor approval
→ n8n test environment
→ limited CRM fields
→ production rollout with audit logs
```

## Repository layout

```text
├── postsite/                    # deterministic Python workflow
├── openclaw-extension/         # Telegram buttons + one-shot scheduling
├── openclaw-skill/             # minimal-question conversation policy
├── tests/                      # Python behavior and safety tests
├── examples/                   # sanitized DMR examples
├── docs/                       # configuration and operations guide
├── api.py                      # optional internal HTTP interface
├── cli.py                      # local testing commands
└── telegram_bot.py             # optional standalone runner, not used with OpenClaw
```

## Privacy

This repository contains only sanitized examples. Keep customer names, addresses, proposal links, employee Telegram handles, group IDs, bot tokens, CRM credentials, `.env`, and SQLite files out of version control. See [SECURITY.md](SECURITY.md).

## Operating principle

The best field workflow is the one technicians actually complete. This agent removes duplicate entry, asks fewer questions, and gives operations timely Telegram visibility without sacrificing the structure needed for a useful CRM record.
