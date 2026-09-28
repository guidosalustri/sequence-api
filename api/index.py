import logging
from dataclasses import asdict
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import FastAPI, Path, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from api.db import Analysis, SequenceRecord, engine
from api.fasta import FastaError, parse_fasta
from api.schemas import AnalysisDetail, AnalysisSummary

# 2 MiB. Vercel rejects request bodies over 4.5 MB before they reach us;
# this lower cap is ours, so oversized files get a clean 413 we control.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# ids are Postgres `integer`; a larger value makes Postgres raise
# "integer out of range" (a 500), so reject it during validation instead.
MAX_ID = 2**31 - 1

logger = logging.getLogger(__name__)

app = FastAPI(title="sequence-api")


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


@app.exception_handler(FastaError)
def handle_fasta_error(request: Request, exc: FastaError):
    return error_response(422, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
def handle_request_validation_error(request: Request, exc: RequestValidationError):
    # Replaces FastAPI's {"detail": [...]} so every error has the same shape.
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"][1:]) or "request"
    return error_response(422, "invalid_request", f"{field}: {first['msg']}")


@app.exception_handler(OperationalError)
def handle_database_unavailable(request: Request, exc: OperationalError):
    # Connection failures and timeouts: worth retrying. Other database errors
    # are bugs and deliberately surface as a plain 500.
    logger.error("database unavailable", exc_info=exc)
    return error_response(
        503, "database_unavailable", "The database is unavailable. Try again in a minute."
    )


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


@app.post("/api/analyze", status_code=201, response_model=AnalysisDetail)
def analyze(file: UploadFile):
    # Read one byte past the limit: if it arrives, the file is too big.
    # Content-Length is client-supplied and covers the whole multipart body.
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return error_response(413, "file_too_large", "The file is larger than 2 MB.")

    records = parse_fasta(data)  # FastaError -> 422 via handle_fasta_error

    # Client-supplied; Postgres can't store NUL, and the column is 255 chars.
    filename = "".join(ch for ch in file.filename or "" if ch.isprintable())[:255]
    analysis = Analysis(
        filename=filename or "unnamed",
        sequence_count=len(records),
        total_length=sum(r.length for r in records),
        sequences=[SequenceRecord(**asdict(r)) for r in records],
    )
    # One transaction: commits at the end of the block, rolls back on error.
    # expire_on_commit=False keeps the RETURNING values (ids, uploaded_at)
    # loaded, so building the response doesn't open a second connection.
    with Session(engine, expire_on_commit=False) as session, session.begin():
        session.add(analysis)
    return analysis


@app.get("/api/analyses", response_model=list[AnalysisSummary])
def list_analyses():
    # Newest first. id follows insert order and is the indexed primary key.
    # Sequences are never loaded: the relationship is lazy and unused here.
    with Session(engine) as session:
        return session.scalars(
            select(Analysis).order_by(Analysis.id.desc()).limit(50)
        ).all()


@app.get("/api/analyses/{analysis_id}", response_model=AnalysisDetail)
def get_analysis(analysis_id: Annotated[int, Path(ge=1, le=MAX_ID)]):
    # Sequences must be loaded before the session closes, because FastAPI
    # serializes after we return. selectinload = one extra indexed query.
    with Session(engine) as session:
        analysis = session.get(
            Analysis, analysis_id, options=[selectinload(Analysis.sequences)]
        )
    if analysis is None:
        return error_response(404, "not_found", f"No analysis with id {analysis_id}.")
    return analysis


# Local dev: serve the frontend from the same origin as the API, as Vercel
# does (its CDN serves public/ before requests reach this app). Must stay
# last: a mount at "/" matches everything, so routes registered after it
# would never be reached. Skipped if public/ isn't in the deployed bundle.
PUBLIC_DIR = FilePath(__file__).resolve().parent.parent / "public"
if PUBLIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
