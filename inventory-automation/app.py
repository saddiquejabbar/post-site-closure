from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from inventory_flow import ParsedRequest, Settings, build_service, render_review

MAX_TELEGRAM_BODY_BYTES = 1_000_000

settings = Settings.from_env()
service = build_service(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await service.telegram.close()


app = FastAPI(
    title="Controlled Inventory Checkout",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "workflow": "telegram-to-legacy-xlsm",
        "writes_enabled": settings.enable_writes,
    }


@app.post("/telegram/webhook", status_code=204)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> None:
    if not settings.webhook_secret_matches(x_telegram_bot_api_secret_token):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    body = await request.body()
    if len(body) > MAX_TELEGRAM_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        update = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="invalid update")
    await service.handle_update(update)


@app.get("/admin/requests/{public_id}")
async def request_status(public_id: str, authorization: str = Header(default="")) -> dict[str, Any]:
    expected = f"Bearer {settings.admin_api_token}" if settings.admin_api_token else ""
    if not expected or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    record = service.store.get(public_id.upper())
    if not record:
        raise HTTPException(status_code=404, detail="not found")
    parsed = ParsedRequest.from_dict(record["parsed"])
    return {
        "public_id": record["public_id"],
        "status": record["status"],
        "created_at_utc": record["created_at_utc"],
        "updated_at_utc": record["updated_at_utc"],
        "summary": render_review(record, parsed),
        "receipt": record.get("receipt"),
        "error": record.get("error"),
    }
