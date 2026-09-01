# Operations runbook

## Initial deployment

1. Install Python 3.11+ and Google Drive for desktop on the designated writer machine.
2. Clone the repository and create a virtual environment.
3. Copy `.env.example` to `.env` and populate secrets locally.
4. Keep the real SKU alias map outside source control or in a protected local path.
5. Copy the production XLSM to a disposable test folder.
6. Keep `WRITE_ENABLED=false`.
7. Run:

```bash
python inventory_cli.py validate-workbook
pytest
```

8. Start the webhook and verify:
   - authorised and unauthorised users;
   - missing-field flags;
   - ambiguous SKU buttons;
   - Replace and Cancel;
   - approver-only approval;
   - duplicate Telegram updates;
   - exact `APPROVED` reply threading.
9. Point to the disposable workbook and set `WRITE_ENABLED=true`.
10. Execute test checkouts and returns. Open the result in desktop Excel after each test.
11. Verify VBA, formulas, pivots, external links, queries, named ranges, and the intended Log row.
12. Test while the workbook is open and during Drive sync. The expected result is a safe refusal, not a forced overwrite.
13. After sign-off, stop the service, update the path to production, and restart.

## Normal operation

- Staff send one complete Telegram message.
- Any flag must be resolved or replaced.
- The approver reviews the full preview and canonical quantities.
- A successful response must say `SAVED AND VERIFIED` and include the workbook row plus short before/after hashes.
- A dry-run response must explicitly say `Workbook changed: NO`.

## Daily checks

- `/inventory_status` shows whether writes are enabled.
- Confirm Google Drive reports the production workbook synced.
- Confirm no stale `.pending` file exists beside the workbook.
- Confirm the backup directory is writable and has free space.
- Review failed requests and audit events in SQLite without editing them manually.

## Safe recovery

### Writer lock exists

Do not delete it immediately. Check whether the recorded PID/service process is still running and whether a write is active. Remove a stale lock only after confirming there is no writer process and the production workbook has a valid hash/open state.

### `Workbook is not stable`

The workbook may be saving, open in Excel, or syncing. Close Excel, allow Drive to settle, and submit a new request. Do not bypass the stability delay.

### Partial blank-Timestamp row

A row has data in `A:HF` but no Timestamp in column C. Inspect and repair that row manually in Excel. The automation must remain stopped until the workbook has one unambiguous next row.

### Header contract changed

Compare row 3 with the documented contract. Update the code/config only after the workbook owner confirms the change. Do not make the parser guess a renamed column.

### Post-write verification failed

The writer attempts restoration only when the current production hash is still the output produced by that request. When a later change is detected, automatic restoration is refused to avoid deleting someone else's work. Escalate with:

- request ID;
- workbook row if known;
- before/after hashes;
- backup filename;
- Drive conflict/version history;
- Excel repair message, if any.

### Duplicate request

The Note cell contains `INVREQ:<request_id>`. A retry returns the existing row without adding another transaction. Never remove these markers from recent rows unless a formal migration replaces the idempotency scheme.

## Rollback

1. Stop the webhook service.
2. Confirm no active writer lock.
3. Compare the production hash with the failed request's recorded after-hash.
4. Preserve a forensic copy of the current production file.
5. Restore the verified backup only when ownership and sequence are clear.
6. Open in desktop Excel and verify before resuming.

## Backup retention

Backups contain business data and must not enter Git. Apply access controls and a retention policy appropriate to the organisation. Retain enough versions to cover Drive conflicts and delayed workbook-repair discovery, then delete securely under policy.
