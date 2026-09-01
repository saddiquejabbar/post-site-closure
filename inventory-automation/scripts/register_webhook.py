from __future__ import annotations

import argparse
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the Telegram inventory webhook")
    parser.add_argument("--base-url", required=True, help="Public HTTPS base URL")
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not token or len(secret) < 16:
        print("Set TELEGRAM_BOT_TOKEN and a 16+ character TELEGRAM_WEBHOOK_SECRET.", file=sys.stderr)
        return 2
    base_url = args.base_url.rstrip("/")
    if not base_url.startswith("https://"):
        print("Webhook base URL must use HTTPS.", file=sys.stderr)
        return 2

    endpoint = f"https://api.telegram.org/bot{token}/setWebhook"
    payload = {
        "url": f"{base_url}/telegram/webhook",
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    try:
        response = httpx.post(endpoint, json=payload, timeout=20)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Webhook registration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if response.status_code >= 400 or not data.get("ok"):
        print(f"Webhook registration rejected: {data.get('description') or response.status_code}", file=sys.stderr)
        return 1
    print("Telegram webhook registered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
