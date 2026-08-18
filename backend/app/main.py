"""FastAPI application entrypoint.

Step 1 exposes health checks only -- the API surface (payment intents,
webhooks) arrives in later steps.
"""

from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session

settings = get_settings()

app = FastAPI(
    title="Payments Ledger & Webhook Engine",
    version="0.1.0",
    description="Double-entry ledger with idempotent intents and verified webhooks.",
)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


@app.get("/health/db", tags=["ops"])
def health_db(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok", "database": "reachable"}
