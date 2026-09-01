# Controlled Telegram → Legacy Excel Inventory Checkout

A small, auditable workflow that turns one natural-language Telegram message into a controlled inventory checkout for a macro-enabled Excel workbook stored in a Google Drive for desktop folder.

> **Provenance:** this is a sanitized reference implementation reconstructed from the confirmed Inventory2 workflow rules. It is not a copy of the live production source. The real workbook, customer records, credentials, Drive identifiers, employee IDs, and private SKU catalogue are intentionally excluded.

## What it does

```text
Telegram message
    ↓
Webhook secret + chat/user allowlists
    ↓
Deterministic parser
    ↓
Current workbook headers + approved alias map
    ↓
Validation and flag queue
    ↓
Telegram candidate / purpose buttons
    ↓
Authorised human approval
    ↓
Single controlled XLSM writer
    ↓
Backup → staged patch → atomic replace → reopen/read-back verification
    ↓
Google Drive desktop sync
```

The system accepts either a structured message or a compact natural sentence. It resolves exact workbook SKU headers and explicitly approved aliases. Fuzzy matches are suggestions only: they can create buttons, but they can never write automatically.

## Example

```text
Checkout by: Alex
Received by: Sam
Customer: Demo Home
Address: 10 Example Road
Quote: Q-1001
Purpose: Install
Items: 2x SKU-SWITCH-1G, 1x zigbee hub
Notes: Packed for tomorrow morning
```

A compact version also works:

```text
Checkout 2x SKU-SWITCH-1G and 1x zigbee hub for Demo Home at 10 Example Road, received by Sam, purpose install
```

When `Checkout By` is omitted, the Telegram sender's display name is used. Returns use negative quantities:

```text
Return 2x SKU-SWITCH-1G for Demo Home at 10 Example Road; received by Sam; purpose return
```

## Flagged requests and buttons

A request stays in `needs_review` when any required field is missing, a quantity is invalid, or an SKU is unknown or ambiguous. Telegram then shows only safe actions:

- exact candidate SKU buttons generated from the current `J:HF` headers;
- purpose buttons for `Install`, `Delivery`, `Servicing`, `Return`, or `Handover`;
- `Replace` to retire the request and submit a corrected full message;
- `Cancel`.

Only the original requester or an approver can resolve flags. Only an approver can use `Approve & write` or reply exactly `APPROVED` to the current preview.

## Confirmed workbook contract

The writer refuses to proceed unless the live workbook still matches this contract:

| Location | Required meaning |
|---|---|
| Sheet | `Log` |
| Row 3 | Header row |
| Row 4 | First possible data row |
| Column A | `Reconciled?` — left blank by automation |
| Column B | `Note` plus idempotency marker |
| Column C | `Timestamp` as an Excel numeric datetime |
| Column D | `Checkout By` |
| Column E | `Received By` |
| Column F | `Quote` |
| Column G | `Name` |
| Column H | `Address` |
| Column I | `Purpose` |
| Columns J:HF | Exact, unique SKU headers and quantities |
| Column HG | `Status` formula preserved or translated from a template row |
| Last allowed row | `9956` |

The next row is the first row whose Timestamp cell is empty. If that row contains any other value or formula in `A:HF`, the writer treats it as a partial/corrupt row and stops instead of overwriting it.

## Safety invariants

1. `WRITE_ENABLED=false` by default.
2. No Telegram message writes directly; every clean request requires separate approval.
3. Every Telegram update, request, and callback action is idempotent.
4. Approval buttons are random, single-use, short-lived tokens; callback payloads contain no customer data.
5. Duplicate SKU lines are combined; returns reduce quantity; net-zero lines are blocked.
6. The current workbook headers are read before parsing and revalidated during writing.
7. A request ID is embedded in the Note cell as `INVREQ:<id>` to prevent duplicate rows after retries.
8. A local exclusive writer lock allows only one process on the designated writer machine.
9. The production file is copied to staging and hashed before modification.
10. A timestamped backup is created outside the Drive-sync directory before replacement.
11. Only the `Log` worksheet XML is patched. Every other XLSM ZIP member must retain the same SHA-256 hash, including `vbaProject.bin`.
12. The completed XLSM is reopened, its contract is checked, and the target row is read back before success is reported.
13. Replacement uses an atomic same-directory rename. A concurrent source change cancels the operation.
14. A hash-chained SQLite audit log records transitions without placing secrets in callback data.

## Quick start

Python 3.11+ is recommended.

```bash
cd inventory-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Populate `.env` with the Telegram IDs and the local path created by Google Drive for desktop. Keep the real alias file and workbook outside Git.

### 1. Test the code

```bash
pytest
python -m compileall -q .
```

### 2. Validate a workbook copy, read-only

```bash
python inventory_cli.py validate-workbook
```

Expected output includes the worksheet member, SKU count, first empty row, date system, status-formula template row, and VBA member count.

### 3. Preview a message, read-only

```bash
python inventory_cli.py preview --text "Checkout by: Alex; received by: Sam; customer: Demo; address: Example; purpose: Install; items: 2x SKU-SWITCH-1G"
```

### 4. Start the webhook locally

```bash
uvicorn app:app --host 127.0.0.1 --port 8088
```

Expose `/telegram/webhook` through approved HTTPS infrastructure, then register it:

```bash
python scripts/register_webhook.py --base-url https://inventory.example.com
```

Telegram sends the configured secret in `X-Telegram-Bot-Api-Secret-Token`; the application compares it in constant time before reading the update.

### 5. Roll out safely

Keep `WRITE_ENABLED=false` while testing the full Telegram flow. Then:

1. place a disposable copy of the real XLSM in a test Drive folder;
2. run `validate-workbook` and several approved test transactions;
3. open the result in desktop Excel and verify macros, pivots, formulas, links, and the written rows;
4. test a duplicate Telegram delivery and a deliberate Drive-sync conflict;
5. designate exactly one writer machine;
6. point to production only after sign-off;
7. change `WRITE_ENABLED=true` and restart the service.

## Google Drive boundary

This implementation writes the local file managed by Google Drive for desktop. It does **not** convert the workbook to Google Sheets or upload it through a generic Drive API because those paths can change or strip macro-enabled workbook behavior.

The local lock protects one machine only. It is not a distributed lock across multiple Drive clients. Operationally, there must be one designated writer, and staff should not keep the workbook open while an approved write is running. A source hash and stability check still stop most concurrent-save and sync races rather than guessing which copy is authoritative.

## Project layout

```text
inventory-automation/
├── app.py                          # FastAPI Telegram webhook
├── inventory_cli.py                # read-only diagnostics and parser preview
├── inventory/
│   ├── catalog.py                  # exact headers, aliases, candidate suggestions
│   ├── config.py                   # environment validation
│   ├── models.py                   # workflow records
│   ├── parser.py                   # deterministic natural-input parser
│   ├── service.py                  # Telegram state machine and approval logic
│   ├── store.py                    # SQLite idempotency, callbacks, audit chain
│   ├── telegram.py                 # minimal Telegram API client
│   ├── validation.py               # blockers and duplicate combining
│   └── workbook.py                 # macro-preserving XLSM controlled writer
├── config/sku_aliases.example.json
├── scripts/register_webhook.py
├── deploy/                         # optional macOS launchd example
├── examples/
└── tests/
```

## Deliberate boundaries

This repository does not contain the production workbook, macros, real SKU catalogue, customer/site records, Telegram credentials, employee IDs, Google Drive paths, or backup files. It does not infer invoice scope, stock availability, or authorisation from an AI model. Natural-language parsing is constrained to converting an operator's checkout instruction into a candidate transaction; deterministic validation and human approval remain the write boundary.
