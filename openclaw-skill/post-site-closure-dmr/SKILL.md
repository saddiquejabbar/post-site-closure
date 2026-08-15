---
name: "post-site-closure-dmr"
description: "Turn a Daily Meeting Report (DMR) into per-site closure questions and a CRM-note draft, in-chat only, no CRM write."
---

# Post-site closure (DMR → closure questions → CRM note draft)

Use this when a message contains a field-operations Daily Meeting Report (DMR) — usually
starts with "Daily Meeting Report" and a date, and lists sites as numbered staff
codes like `1+2. Customer Name` or `6. Customer Name`. Also use it for follow-up
replies inside an already-started closure thread for one of these sites.

This is a **test flow**. Never post to a CRM, call a webhook, or claim data was
saved anywhere outside this chat. Only ever produce: (1) closure questions, and
(2) a plain-text CRM-note draft shown in the chat for an authorized operations reviewer.

## Telegram control boundary

The local `post-site-closure-inbound` extension—not the model—handles the exact
`run post-site` trigger, parses the DMR with the Python project, displays the
site/time/staff preview, emits native approval buttons, and creates exact
one-shot OpenClaw check-in jobs after approval.

- Do not discuss Telegram bot tokens, BotFather, second pollers, or 409 conflicts.
- Do not ask the user to choose between cron and a standalone bot.
- Do not manually create post-site cron jobs during an agent/model turn.
- Do not claim a DMR preview is scheduled. Only the approval callback can schedule it.
- Use this skill when an installer replies to a scheduled site check-in, and for
  the minimum-question conversation that ends in `CRM NOTE — DRAFT ONLY`.

## 1. Parse the DMR

For each entry:
- Header line: `N[+N...]. Deal Name [- time]` — the leading numbers are staff
  codes, and a trailing time such as `- 10:30am`, `- 12pm`, or `- 5pm` is the
  exact approved check-in time in Singapore time. Strip the time from the deal
  name after parsing it.
- Staff codes are configured with `STAFF_N_NAME` and `STAFF_N_MENTION` environment variables. Use a real
  Telegram @mention only when one is configured; never invent usernames.
- Next line: `📍 Location | Activity Type | optional flags`.
- Following lines are work items until the next numbered entry. A short line
  ending in `:` (≤4 words) becomes a section label prefixed to the items under it
  (e.g. "Install:" makes later items "Install: 8x top hung").
- A line containing "proposal" + a URL is the proposal link — keep for reference,
  don't need to surface it unless asked.
- If any work item contains "collect payment" (case-insensitive), payment is
  required for that site and gets asked about last.

Normalize Activity Type to a visit type:
- contains "servic" → **service**
- contains "handover" and not "install" → **handover**
- contains "install" → **installation**
- contains "deliver" → **delivery**
- contains any of "site discussion", "site meeting", "site visit", "assess" → **assessment**
- contains "test" → **testing**
- otherwise → **other**

## 2. Ask the primary closure question, one site at a time (or batched, the operations lead's call)

- installation: "Was everything installed/completed as planned? Reply YES, or tell me what was not completed."
- service: "Was the issue resolved? Reply with what you found/did, or NO if it is still unresolved."
- handover: "Was the handover completed and the system working? Reply YES, or tell me what is still outstanding."
- delivery: "Were all items delivered? Reply YES + who received them, or tell me what was missing/damaged."
- assessment: "What was agreed/found on site, and what is the next action? One short reply is enough."
- testing: "What was tested and what was the result? Include any follow-up needed."
- other: "Was this completed? Reply YES, or tell me what remains outstanding."

## 3. Follow-up rules (ask at most what's genuinely missing)

- **service** or **delivery**: even a bare YES gets exactly one follow-up —
  service: "What did you find/do to resolve it? One short reply." /
  delivery: "Who received the items on site?"
- Treat a reply as **incomplete** if it contains words like pending / outstanding /
  missing / damaged / failed / unable / unresolved / incomplete, or "not/isn't/
  wasn't/still installed/completed/working/resolved/online/offline/done/ready",
  or "can't/cannot/couldn't", or "need(s) to return/come back" — unless it's
  phrased as "no items outstanding/pending/missing/damaged" (which is a positive).
- If incomplete AND no next step is mentioned (no words like return, come back,
  next visit, follow-up, replace, order, await, waiting, schedule, reschedule,
  need(s), require(s), electrician, contractor, owner to, stock), ask: "What is
  needed next? If another visit is required, include what was told to the owner."
- Detailed replies are kept close to verbatim in the note — don't over-interpret
  or paraphrase away specifics.
- If payment was flagged required and not yet covered: "Was payment collected?
  Reply YES/NO. If needed, add a short note." If NO: "Why was payment not
  collected?"
- Once nothing is missing, stop asking and produce the note.

## 4. CRM note draft (plain text, shown in chat, never sent anywhere)

Format, one line per site:

```
{Label}: {planned work summary}. {Completed as planned.|Not fully completed.}
[Result|Outcome|Delivery|Received by|Outstanding|Next action: ...]
[Payment: Collected.|Payment: Not collected — {reason}.]
```

Label = Installation / Servicing / Handover / Delivery / Site assessment /
Testing / the raw activity text for "other". Planned work summary = the work
items joined with "; ", excluding the "collect payment" line itself.

Prefix each note with something like `CRM note (draft, not sent):` so it's clear
this is a sample, not a real CRM write — so operators can review it before any optional CRM integration.

## Guardrails

- Never call `CRM_WEBHOOK_URL`, never claim a CRM record was created/updated.
- Never invent installed items, outcomes, or payment status that weren't in the
  DMR or a reply — ask instead.
- Keep every group isolated by its configured Telegram group ID and authorized-sender list.
