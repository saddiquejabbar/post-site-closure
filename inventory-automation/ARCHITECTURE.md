# Architecture and control model

## Core principle

Natural language is an input convenience, not an authorisation mechanism. The parser may propose a transaction. Deterministic controls and a separate human approval decide whether a write can occur.

## Components

### FastAPI webhook

`app.py` exposes `/telegram/webhook`. It rejects requests unless Telegram's secret-token header matches the configured value in constant time. Request bodies are capped at 1 MB.

### Telegram boundary

The service checks both the chat ID and sender ID. Requesters and approvers are separate allowlists. An unauthorised tap does not consume the button token, preventing a group member from invalidating an approver's action.

### Parser and SKU catalogue

The parser supports labelled fields and a compact sentence. It does not call an LLM. SKU resolution order is:

1. exact `J:HF` workbook header;
2. normalized exact header;
3. explicit alias from the local JSON map;
4. fuzzy candidate suggestion only.

Ambiguity always blocks. Candidate buttons preserve the exact workbook header selected by a human.

### Validation

Validation blocks:

- missing required metadata;
- no item lines;
- unknown or ambiguous SKUs;
- zero, non-finite, or over-limit quantities;
- too many item lines;
- overlong fields;
- duplicate/return combinations that net to zero.

Duplicate canonical SKUs are combined into one cell value.

### SQLite workflow store

SQLite owns the durable workflow state:

```text
received → needs_review → awaiting_approval → approved → writing
                                                    ↘ dry_run_complete
                                                    ↘ completed
                                                    ↘ failed
```

`cancelled` and `superseded` are terminal side paths. Status changes use compare-and-set transitions, so two approvals cannot both claim the same request.

The store contains:

- processed Telegram update IDs;
- request source, preview, parsed data, validation, status, and write result;
- random callback tokens with expiry and single-use timestamps;
- a SHA-256 hash-chained audit event table.

### Controlled XLSM writer

The workbook is an OPC ZIP package. The writer changes only the XML member that represents the configured `Log` worksheet. It deliberately avoids a general spreadsheet re-save, which can recalculate, normalize, or alter unrelated macro-enabled workbook parts.

## Write transaction

1. Acquire a local exclusive lock using `O_CREAT | O_EXCL`.
2. Wait briefly and verify the source size, mtime, and inode are stable.
3. Hash the production XLSM.
4. Copy it to a request-specific staging directory and verify the copied hash.
5. Open the package and validate:
   - exactly one `Log` sheet;
   - exact metadata headers in `A:I`;
   - nonblank, unique SKU headers in `J:HF`;
   - `Status` in `HG`;
   - at least one `vbaProject.bin` member when required;
   - an available row at or before row 9956;
   - no partially populated blank-Timestamp row.
6. Search `Note` for `INVREQ:<request_id>`. If found, return the existing row as an idempotent success.
7. Patch one row:
   - leave `Reconciled?` blank;
   - write an Excel numeric local datetime;
   - write metadata as inline strings;
   - write canonical quantities under exact current headers;
   - preserve or row-translate the HG formula;
   - remove the stale cached formula value.
8. Build a staged XLSM with the same member list.
9. Compare pre/post member hashes. Only the Log worksheet XML may differ; VBA and all other members must match.
10. Reopen the staged workbook and read the written row back.
11. Re-hash production; abort if another process or Drive changed it.
12. Create and verify a timestamped backup outside the sync directory.
13. copy staged output to a same-directory pending file and fsync it.
14. Re-hash production again, then atomically replace it with `os.replace`.
15. Reopen production, validate the contract, and read the row back again.
16. Report success only after verification. If post-replace verification fails and the file is still the exact output owned by this transaction, restore the backup.

## Why the writer does not use the Google Drive API

A macro-enabled workbook should remain a binary Excel package. This design treats Google Drive for desktop as the sync transport and performs the mutation on the local file. It avoids conversion to Google Sheets and keeps the write operation compatible with same-directory atomic replacement.

## Concurrency boundary

The local lock is process-safe on one machine, not distributed across Drive clients. The architecture therefore requires one designated writer machine. Source stability checks and repeated SHA-256 comparisons reject detected races; they do not make multiple Drive clients safe writers.

## Failure policy

The system fails closed. Examples:

- unknown SKU → review buttons, no write;
- changed header → block;
- partial row → block;
- missing macro binary → block;
- workbook changed during staging → block;
- backup hash mismatch → block;
- any unrelated XLSM member changes → block;
- read-back mismatch → failure and conditional restore;
- duplicate webhook or approval → idempotent no-op or existing-row success.
