import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.db import engine

logger = logging.getLogger(__name__)

app = FastAPI(title="sequence-api")


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
