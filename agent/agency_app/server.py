"""FastAPI server for the agency mini app.

  GET  /api/health       — liveness
  GET  /api/suggestions  — top 10 pending for the authed user
  POST /api/swipe        — record a swipe; right-swipe dispatches the action
  GET  /                 — static SPA (index.html)

Auth: every /api/* request (except /api/health) must carry
  Authorization: tma <Telegram WebApp initData>
The HMAC is verified against TG_BOT_TOKEN and the resolved user_id is
checked against the box's owner allowlist.

Demo mode: when env BUX_AGENCY_DEMO=1, requests with `?demo=1` skip auth and
scope to user_id="demo". Useful for `uvicorn --reload` browser smoke testing.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, dispatch, seeder

LOG = logging.getLogger("agency.server")
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).parent / "static"
DEMO_ENABLED = os.environ.get("BUX_AGENCY_DEMO", "").lower() in ("1", "true", "yes")

app = FastAPI(title="bux agency mini app", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    db.init()
    if DEMO_ENABLED and db.count_pending("demo") == 0:
        seeder.seed_demo("demo")


# ---- auth dependency ----

class AuthCtx(BaseModel):
    user_id: str
    is_demo: bool = False


def _resolve_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    demo: Annotated[int, Query()] = 0,
) -> AuthCtx:
    if DEMO_ENABLED and demo == 1:
        return AuthCtx(user_id="demo", is_demo=True)
    init_data = auth.parse_authorization(authorization)
    if not init_data:
        raise HTTPException(status_code=401, detail="missing initData")
    token = auth.bot_token()
    if not token:
        LOG.error("TG_BOT_TOKEN not configured; cannot validate initData")
        raise HTTPException(status_code=503, detail="bot token unavailable")
    user = auth.verify_init_data(init_data, token)
    if not user:
        raise HTTPException(status_code=401, detail="bad initData signature")
    user_id = str(user["id"])
    if user_id not in auth.allowlisted_user_ids():
        raise HTTPException(status_code=403, detail="user not allowlisted")
    return AuthCtx(user_id=user_id)


# ---- API ----

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "demo": DEMO_ENABLED}


@app.get("/api/suggestions")
def list_suggestions(ctx: Annotated[AuthCtx, Depends(_resolve_user)]) -> dict:
    items = db.list_pending(ctx.user_id, limit=10)
    seeded = seeder.maybe_refill(ctx.user_id) if not ctx.is_demo else False
    return {"items": items, "refilling": seeded}


class SwipeBody(BaseModel):
    suggestion_id: int
    decision: str = Field(pattern="^(right|left|up)$")
    feedback_text: str | None = None


@app.post("/api/swipe")
def swipe(
    body: SwipeBody,
    ctx: Annotated[AuthCtx, Depends(_resolve_user)],
) -> dict:
    suggestion = db.get_suggestion(body.suggestion_id, ctx.user_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    if suggestion["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"already {suggestion['status']}")

    db.insert_decision(body.suggestion_id, ctx.user_id, body.decision, body.feedback_text)
    new_status = {"right": "accepted", "left": "dismissed", "up": "feedback"}[body.decision]
    db.update_status(body.suggestion_id, ctx.user_id, new_status)

    dispatched = False
    if body.decision == "right" and not ctx.is_demo:
        task_id = db.insert_task(body.suggestion_id, ctx.user_id)
        dispatched, topic_id = dispatch.dispatch_action(
            user_id=ctx.user_id,
            suggestion_title=suggestion["title"],
            draft_action=suggestion["draft_action"] or suggestion["title"],
        )
        if topic_id is not None:
            db.set_task_topic(task_id, topic_id)

    if body.decision == "up" and body.feedback_text and not ctx.is_demo:
        # Mid-stream feedback as a fresh suggestion the agent can re-propose
        # against. V2 will revise the original and re-queue it; for V1 we
        # just record the user's note and surface it on the next refill.
        revision_title = f"Revise: {suggestion['title'][:60]}"
        revision_body = (
            f"Original: {suggestion['title']}\n"
            f"User feedback: {body.feedback_text}\n\n"
            f"Original draft action: {suggestion['draft_action']}"
        )
        db.insert_suggestion(
            user_id=ctx.user_id,
            title=revision_title,
            description=revision_body,
            draft_action=f"Revise the previous proposal based on this feedback: {body.feedback_text}",
            source="up-revision",
        )

    refilling = False
    if not ctx.is_demo and db.count_pending(ctx.user_id) < 10:
        refilling = seeder.maybe_refill(ctx.user_id)

    return {
        "ok": True,
        "status": new_status,
        "dispatched": dispatched,
        "refilling": refilling,
    }


# ---- static SPA ----

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.exception_handler(HTTPException)
def _http_exc(_request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
