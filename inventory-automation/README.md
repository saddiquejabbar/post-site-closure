# Controlled Inventory Checkout

**One Telegram message → validated preview → human approval → controlled append into a legacy macro-enabled Excel workbook shared through Google Drive.**

This is a sanitized reference implementation of an operations workflow. It contains no real customers, employee IDs, credentials, Google Drive IDs, workbook, product catalog or employer data.

## The simple operator experience

Send one message:

```text
Checkout 2x Atlas 75 white, 1x Zigbee hub | customer Tan | site Punggol | installer Hasan | job ZD-1042
```

The bot replies with a structured preview:

```text
Inventory request INV-7A91C20F
Status: PENDING APPROVAL

Customer: Tan
Site: Punggol
Installer: Hasan
Job ref: ZD-1042

Items:
1. 2 x SW-75-W-ZB — Atlas 75 White Zigbee Switch
2. 1 x HUB-ZB-01 — Zigbee Hub

Validated. No workbook change yet.
```

An approved controller then selects:

- **Approve & write**
- **Reject**

If the input is ambiguous, the bot does not guess. For `2x Atlas 75`, it shows one button per matching SKU, such as **White** or **Black**. Missing customer, site, installer, job reference, quantity or unknown SKU keeps the request in the flagged queue.

## Why this design

The workbook is a legacy operational asset, not a disposable database. It may contain macros, formulas, formatting, external links, controls and years of history. Treating it like a normal spreadsheet and re-saving the whole file creates unnecessary corruption risk.

This workflow separates three responsibilities:

1. **Interpretation:** convert one human message into a proposed structured checkout.
2. **Decision:** show exactly what will be written and require an approved human action.
3. **Execution:** append only approved rows through a guarded, idempotent transaction.

The parser is deterministic in this version. It can be replaced by an LLM later, but any model should only propose structured data. A model must never receive direct workbook write authority.

## Architecture

```text
Telegram message
      ↓
Telegram webhook secret validation
      ↓
Allowed-chat check
      ↓
Deterministic parser + controlled SKU catalog
      ↓
SQLite request state and duplicate-update protection
      ↓
Telegram review card
      ├── exact request → Approve / Reject
      └── flagged request → SKU choice / Correction / Reject
      ↓
Approver-ID authorization
      ↓
Safe XLSM transaction
      ├── process lock
      ├── Excel-open lock check
      ├── source SHA-256
      ├── timestamped backup outside Google Drive
      ├── header and sheet-layout validation
      ├── workbook row-ID idempotency check
      ├── modify one worksheet XML part only
      ├── verify every non-target package member is unchanged
      ├── verify vbaProject.bin is unchanged
      ├── recheck live source hash
      └── atomic replace + receipt
      ↓
Google Drive for Desktop syncs the committed workbook
```

See [ARCHITECTURE.md](ARCHITECTURE.md) and [SAFE_WRITE_PROTOCOL.md](SAFE_WRITE_PROTOCOL.md) for the exact state machine and transaction rules.

## Main controls

| Control | Failure prevented |
|---|---|
| Telegram secret-token comparison | Unauthenticated webhook submissions |
| Allowed group IDs | Bot use from an unintended chat |
| Approver user-ID allowlist | Any group member authorizing a write |
| Deterministic catalog matching | Invented or hallucinated SKUs |
| Required hard job reference | Checkout rows that cannot be tied to a job |
| Flagged candidate buttons | Silent choice between similar products |
| SQLite unique Telegram update ID | Duplicate webhook delivery creating duplicate requests |
| Workbook row IDs | Duplicate rows after retry or process crash |
| Header validation | Writing into the wrong columns or workbook version |
| Macro-project hash verification | Accidental macro modification |
| Non-target package hash verification | Collateral workbook-part damage |
| Source-hash recheck | Overwriting a workbook changed during the transaction |
| Atomic replacement | Leaving a half-written live workbook |
| Timestamped backup | Fast operator rollback |
| `ENABLE_WRITES=false` by default | Accidental production write during setup |

## Repository layout

```text
inventory-automation/
├── app.py                         # FastAPI webhook and status endpoint
├── inventory_flow.py              # parser, catalog, state, Telegram review and approval
├── safe_xlsm.py                   # guarded append-only XLSM transaction
├── examples/
│   ├── catalog.example.json
│   ├── workbook_mapping.example.json
│   └── telegram_examples.md
├── tests/
│   ├── test_inventory_flow.py
│   └── test_safe_xlsm.py
├── ARCHITECTURE.md
├── SAFE_WRITE_PROTOCOL.md
├── SECURITY.md
├── .env.example
├── requirements.txt
└── requirements-dev.txt
```

## Setup

### 1. Use a test copy first

Never begin with the live workbook. Make a sanitized copy of the `.xlsm` file, confirm it opens normally in desktop Excel, and point `WORKBOOK_PATH` to that copy.

The target worksheet must be a plain append-only log sheet. This version deliberately refuses a protected target sheet or a target sheet containing an Excel structured table.

### 2. Configure the SKU catalog

Copy `examples/catalog.example.json` and replace the examples with approved SKUs and aliases. Multiple products may intentionally share a vague alias. That creates a Telegram selection rather than a guess.

### 3. Configure the workbook mapping

Copy `examples/workbook_mapping.example.json` and map each approved field to its real column. `expected_headers` are mandatory layout fingerprints. If a header changes, the write fails before the live file is touched.

Keep an **Automation Row ID** column. It is the durable idempotency key that makes a retry safe even when the application crashes after Excel was updated but before SQLite was updated.

### 4. Configure environment variables

```bash
cd inventory-automation
cp .env.example .env
```

Set the workbook to the local Google Drive for Desktop path. Keep `BACKUP_DIR` outside the synced Drive folder.

`ENABLE_WRITES` must remain `false` during setup and dry-run review.

### 5. Install and test

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

The included tests verify exact parsing, ambiguity flagging, missing-field rejection, macro preservation, non-target package preservation, header mismatch rejection and idempotent replay.

### 6. Run the webhook service

```bash
uvicorn app:app --host 127.0.0.1 --port 8080 --env-file .env
```

Expose it only through an authenticated HTTPS reverse proxy or controlled tunnel. Set the Telegram webhook with the same random secret stored in `TELEGRAM_WEBHOOK_SECRET`:

```bash
curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://inventory.example.com/telegram/webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

### 7. Validate before enabling writes

With `ENABLE_WRITES=false`:

1. Send exact, ambiguous, incomplete and duplicate examples.
2. Confirm only approved Telegram IDs can operate buttons.
3. Confirm the review card matches the intended rows.
4. Confirm no workbook change occurs.

Then test against a copied workbook with writes enabled. Open the before and after files in desktop Excel, run the existing macros, check formulas and formatting, inspect the appended rows, and verify the backup.

Only after those acceptance tests should the production service use:

```text
ENABLE_WRITES=true
```

## Google Drive operating model

This implementation writes to the local path maintained by **Google Drive for Desktop**. That is intentional: the workbook transaction requires filesystem locking, hashing and atomic replacement.

Google Drive is not a distributed workbook lock. Operational rules still matter:

- Close the workbook before approving a write.
- Use one automation writer only.
- Do not let two machines run this service against the same shared file.
- Investigate Drive conflict copies immediately.
- Monitor the Drive client separately; a local commit receipt proves the local file transaction, not successful cloud synchronization.

For environments requiring authoritative remote locking and sync confirmation, move the source of truth to a database or controlled API and generate Excel as an output. A shared `.xlsm` file is a compatibility boundary, not an ideal transaction system.

## Deliberate limitations

- It does not infer missing facts.
- It does not scrape product data or customer systems.
- It does not publish or include the real workbook.
- It does not update Excel tables, PivotTables or table ranges.
- It does not execute or inspect VBA.
- It does not guarantee Google Drive cloud sync.
- It does not make a shared workbook safe for concurrent human editing.
- It refuses large writes; this is an operational checkout workflow, not a bulk migration tool.

## Production recommendation

Use this implementation as the controlled edge around the legacy workbook. The higher-value long-term architecture is:

```text
Telegram / form / API
        ↓
validated inventory transaction database
        ↓
audit and approval service
        ↓
Excel compatibility export or controlled append
```

That removes the `.xlsm` file as the only source of truth while preserving the existing macro workflow during transition.
