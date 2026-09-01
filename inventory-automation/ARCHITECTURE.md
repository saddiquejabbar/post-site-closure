# Architecture and State Ownership

## Goal

Accept one compact inventory checkout instruction from Telegram, convert it into a reviewable transaction, and append approved rows to a legacy macro-enabled workbook without giving free-form input direct write authority.

## Trust boundaries

### Untrusted

- Telegram message text
- Product nicknames and abbreviations
- Duplicate webhook deliveries
- Callback data supplied by a client
- The assumption that a Google Drive file is unchanged

### Trusted only after explicit checks

- Telegram update with the configured webhook secret
- Message from an allowed chat ID
- Button action from an approved Telegram user ID
- SKU selected from the local approved catalog
- Workbook matching the configured sheet and header fingerprint
- Candidate file passing package and idempotency validation

### Never trusted as a security boundary

- Prompt instructions
- An LLM's confidence
- File name alone
- Google Drive synchronization state
- A Telegram display name or username

## Components

### `app.py`

- Receives Telegram webhook updates.
- Compares `X-Telegram-Bot-Api-Secret-Token` with the configured secret.
- Rejects oversized or malformed payloads.
- Exposes a token-protected read-only request status endpoint.

### `InventoryParser`

- Parses the recommended natural single-message format.
- Requires customer, site, installer and job reference.
- Resolves only catalog-backed SKUs.
- Produces candidates when an alias maps to multiple products.
- Flags unknown items and missing quantities.

### `RequestStore`

SQLite owns workflow state. The workbook owns committed inventory rows.

Stored fields include:

- public request ID
- unique Telegram update ID
- source chat and message IDs
- raw input
- parsed proposal
- workflow status
- review message ID
- approver ID
- write receipt or failure
- timestamps

A stale `writing` state is converted to `failed` at startup. Retrying is safe because the workbook row IDs are checked before any new append.

### `InventoryService`

- Sends the review card.
- Builds inline SKU-selection, approval and rejection buttons.
- Revalidates the actor against the approver-ID allowlist.
- Claims one request atomically before writing.
- Converts one approved request into one row per SKU.

### `SafeXlsmWriter`

- Owns the only workbook write path.
- Acquires an advisory process lock.
- Fails when Excel's owner lock file exists.
- Creates a backup.
- validates the worksheet mapping.
- checks durable row IDs.
- changes one worksheet XML part.
- verifies every other ZIP package member, including the VBA project, remains byte-identical when decompressed.
- replaces the live workbook atomically only if the source hash is unchanged.

## State machine

```text
                    ┌─────────────┐
Telegram input ───▶ │   flagged   │
                    └──────┬──────┘
                           │ resolve every flag
                           ▼
                    ┌──────────────────┐
                    │ pending_approval │
                    └──────┬───────────┘
                           │ approver button
                           ▼
                    ┌─────────────┐
                    │   writing   │
                    └───┬─────┬───┘
                        │     │
                  success     failure / stale recovery
                        │     │
                        ▼     ▼
                 ┌──────────┐ ┌────────┐
                 │committed │ │ failed │─── safe retry ──▶ writing
                 └──────────┘ └────────┘

flagged / pending_approval / failed ── reject ──▶ rejected
```

Terminal states are `committed` and `rejected`.

## Idempotency layers

1. **Telegram layer:** `telegram_update_id` is unique in SQLite. Telegram can redeliver a webhook without creating another request.
2. **Workflow layer:** only one transition can claim `pending_approval` or `failed` as `writing`.
3. **Workbook layer:** every output row has a durable ID such as `INV-7A91C20F:01`.
   - All row IDs present: treat as a successful replay; append nothing.
   - No row IDs present: proceed.
   - Some row IDs present: stop with `PartialCommitDetected`; require investigation.

The workbook layer is decisive after a process crash because SQLite and Excel cannot participate in one shared database transaction.

## Approval model

The requester and approver may be the same person operationally, but the permission check is based on immutable Telegram numeric user IDs, not names.

Button callback data contains only:

```text
workflow prefix + public request ID + action + candidate indexes
```

The server reloads the canonical request and candidate list. It never trusts a SKU supplied directly by callback data.

## Google Drive boundary

The service targets a local Google Drive for Desktop path. The safe transaction ends after local atomic replacement and receipt generation. Cloud synchronization is a separate process and must be monitored separately.

This prevents the application from claiming stronger consistency than Google Drive provides. If distributed concurrent editing is required, the inventory source of truth should move to a database.

## Adding an LLM safely

An LLM may be inserted only before validation:

```text
raw message → LLM proposal → strict schema validation → catalog resolution → review queue
```

Required controls:

- temperature near zero
- JSON schema output
- no direct file or shell tool
- no ability to approve its own result
- every SKU re-resolved against the deterministic catalog
- missing data remains missing
- low confidence always flags

The deterministic parser should remain available as a fallback and test oracle.
