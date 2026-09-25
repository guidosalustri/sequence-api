import logging
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

app = FastAPI(title="sequence-api")


def _make_engine() -> Engine | None:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    # Neon issues postgresql:// URLs, which SQLAlchemy maps to psycopg2.
    # Point them at psycopg 3, the driver we actually install.
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
            break
    # Serverless: no pooling in-process; Neon's pooler handles it.
    # connect_timeout (seconds) makes an unreachable DB fail fast instead of
    # hanging until the function is killed; 10s leaves room for Neon cold starts.
    return create_engine(
        url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 10},
    )


engine = _make_engine()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/db-check")
def db_check():
    if engine is None:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "DATABASE_URL is not set"},
        )
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1")).scalar_one()
    except SQLAlchemyError as exc:
        logger.exception("db-check failed")
        error = type(getattr(exc, "orig", None) or exc).__name__
        return JSONResponse(status_code=503, content={"status": "error", "error": error})
    return {"status": "ok", "result": result}
