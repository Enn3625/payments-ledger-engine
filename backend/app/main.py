"""FastAPI application entrypoint.

Step 1 exposes health checks only -- the API surface (payment intents,
webhooks) arrives in later steps.
"""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import anomaly_flags, auth, ledger, payment_intents, webhooks
from app.config import get_settings
from app.db import get_session

settings = get_settings()

app = FastAPI(
    title="Payments Ledger & Webhook Engine",
    version="0.1.0",
    description="Double-entry ledger with idempotent intents and verified webhooks.",
)


# The dashboard runs on its own origin, so the browser needs an explicit
# allowance. Origins are configured rather than wildcarded: `*` would let any
# site on the internet call this API with a user's token.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Idempotent-Replay"],
)

app.include_router(anomaly_flags.router)
app.include_router(auth.router)
app.include_router(ledger.router)
app.include_router(payment_intents.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


@app.get("/health/db", tags=["ops"])
def health_db(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "reachable"}
