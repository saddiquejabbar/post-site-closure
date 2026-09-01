# Security and privacy

## Never commit

- Telegram bot tokens, API keys, webhook secrets, or CRM credentials
- Real customer names, addresses, phone numbers, proposal links, or access codes
- Employee Telegram IDs or usernames
- Telegram group IDs and authorized sender IDs
- `.env`, SQLite databases, logs, exports, or raw production DMRs

Use the sanitized examples and placeholder IDs included in this repository.

## Deployment boundaries

- Restrict the extension to one explicit Telegram group ID.
- Restrict DMR ingestion and schedule approval to explicit sender IDs.
- Keep the OpenClaw gateway on a trusted host and keep its configuration private.
- Run only one Telegram poller for a bot token.
- Leave `CRM_WEBHOOK_URL` blank during a draft-only pilot.
- Review CRM field mappings and webhook authentication before enabling writes.

## Reporting a vulnerability

Do not open a public issue containing secrets or production data. Contact the repository owner privately with a minimal, sanitized reproduction.
