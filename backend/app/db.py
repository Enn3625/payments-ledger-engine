"""Engine and session plumbing.

The ledger uses synchronous SQLAlchemy on psycopg3. Correctness work here leans
on explicit transaction boundaries, row locks and deferred constraint triggers;
a sync session keeps those semantics obvious, and FastAPI runs plain `def`
endpoints in a threadpool anyway.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session (and one DB transaction) per request."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
