from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from postsite.config import settings
from postsite.dmr import parse_dmr
from postsite.service import ClosureService
from postsite.store import Store

settings.ensure_data_dir()
store = Store(settings.db_path)
service = ClosureService(store)
app = FastAPI(title="Post-Site Closure Agent", version="0.1.0")


class DMRIn(BaseModel):
    text: str
    date: str | None = None


class ReplyIn(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/dmr")
def ingest_dmr(body: DMRIn):
    sites = store.upsert_sites(parse_dmr(body.text, fallback_date=body.date))
    return {"count": len(sites), "sites": [s.to_dict() for s in sites]}


@app.get("/sites")
def list_sites(date: str | None = None, status: str | None = None):
    return [s.to_dict() for s in store.list(dmr_date=date, status=status)]


@app.get("/sites/{site_id}/prompt")
def get_prompt(site_id: int):
    site = store.get(site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    return {"site_id": site.id, "prompt": service.prompt_text(site)}


@app.post("/sites/{site_id}/reply")
def record_reply(site_id: int, body: ReplyIn):
    try:
        site, question = service.record_reply(site_id, body.text)
    except KeyError:
        raise HTTPException(404, "Site not found")
    return {
        "site": site.to_dict(),
        "next_question": question,
        "crm_payload": service.payload(site) if not question else None,
    }


@app.get("/sites/{site_id}/crm-payload")
def crm_payload(site_id: int):
    site = store.get(site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    return service.payload(site)
