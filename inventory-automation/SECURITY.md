# Security and Data Handling

## Public repository rule

Do not commit:

- the real `Inventory2.xlsm` workbook or any derivative;
- customer names, addresses, phone numbers or job references;
- employee Telegram IDs or handles;
- the real SKU catalog if it is proprietary;
- Telegram bot tokens or webhook secrets;
- Google Drive paths, file IDs or service-account credentials;
- SQLite state, logs, backups or webhook captures.

The supplied catalog, people, sites and job references are fictional examples.

## Secrets

Store secrets in `.env` or an operating-system secret manager. `.env` is gitignored.

Use separate random values for:

- `TELEGRAM_WEBHOOK_SECRET`
- `ADMIN_API_TOKEN`

Rotate the Telegram bot token immediately if it is exposed. A token embedded in Git history should be treated as compromised even after the file is deleted.

## Telegram controls

- Set Telegram's webhook `secret_token` and validate its header on every request.
- Restrict accepted messages to `ALLOWED_TELEGRAM_CHAT_IDS`.
- Restrict approval callbacks to numeric `APPROVER_TELEGRAM_IDS`.
- Do not authorize by Telegram username or display name.
- Keep bot privacy and group permissions as narrow as the workflow permits.
- Do not grant the bot unrelated administrative rights.

The webhook secret authenticates the Telegram delivery path; it does not validate the business content. Catalog, field and approval validation still apply.

## Host permissions

Run the service under a dedicated local account where practical. Grant it access only to:

- the one workbook path;
- the backup directory;
- the SQLite state directory;
- the catalog and mapping files.

Do not give the process broad Google Drive or home-directory access.

## Workbook and Drive controls

- Keep backups outside the Google Drive sync folder.
- Limit Drive access to the operational group that already requires the workbook.
- Keep desktop Excel closed during an approved write.
- Run only one writer against the shared file.
- Monitor Google Drive conflict copies and sync failures.
- Periodically restore a backup in a test location and open it in Excel.

## Data retention

SQLite stores raw Telegram text and structured request details. Treat the database as operationally sensitive.

Define a retention period appropriate to the company. A practical policy is to keep committed transaction metadata long enough for inventory reconciliation while deleting rejected or malformed raw messages sooner.

## Logging

Do not log:

- bot tokens;
- webhook or admin secrets;
- full webhook headers;
- the entire workbook;
- private catalog contents unnecessarily.

Errors shown in Telegram are intentionally truncated. Detailed operational errors remain local.

## Reporting a vulnerability

Do not open a public issue containing credentials, workbook samples or customer data. Revoke exposed credentials first, preserve evidence privately, and contact the repository owner through a private channel.
