import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path as FilePath
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Path, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from api.db import Analysis, NcbiCache, SequenceRecord, engine
from api.fasta import FastaError, parse_fasta
from api.ncbi import NcbiUnavailable, Summary, extract_accession, fetch_summaries
from api.schemas import AnalysisDetail, AnalysisSummary, NcbiEnrichment, NcbiRecord

# 2 MiB. Vercel rejects request bodies over 4.5 MB before they reach us;
# this lower cap is ours, so oversized files get a clean 413 we control.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

# ids are Postgres `integer`; a larger value makes Postgres raise
# "integer out of range" (a 500), so reject it during validation instead.
MAX_ID = 2**31 - 1

# Record annotations change occasionally even within a version, so cached
# lookups (found or not) are refetched after this long.
NCBI_CACHE_TTL = timedelta(days=30)
# Records looked up per analysis: the same 200 the page renders. Also keeps
# one esummary request's URL a sensible length.
MAX_NCBI_LOOKUPS = 200

# Storage caps: every table a stranger can grow has one, so no one can fill
# Neon's free tier (~512 MB for the whole project). Oldest rows go first, so
# the demo keeps working instead of refusing uploads once full.
# Worst-case analysis measured at 3.06 MiB (10,000 records, 2 MiB of headers):
# 50 of them is ~153 MiB. 50 is also exactly what the history list shows.
MAX_STORED_ANALYSES = 50
MAX_NCBI_CACHE_ROWS = 20_000  # ~5 MB

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
        session.flush()  # insert now, so the new analysis counts toward the cap
        # Delete everything at or below the (cap+1)-th newest id; with fewer
        # analyses than that, the subquery is NULL and nothing matches.
        # Their sequences go too, via ON DELETE CASCADE (fast thanks to the
        # index on sequences.analysis_id).
        cutoff = (
            select(Analysis.id)
            .order_by(Analysis.id.desc())
            .offset(MAX_STORED_ANALYSES)
            .limit(1)
            .scalar_subquery()
        )
        session.execute(delete(Analysis).where(Analysis.id <= cutoff))
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


@app.get("/api/analyses/{analysis_id}/ncbi", response_model=NcbiEnrichment)
def ncbi_enrichment(analysis_id: Annotated[int, Path(ge=1, le=MAX_ID)]):
    # 1. Read everything needed from the database, then release the connection.
    cutoff = datetime.now(timezone.utc) - NCBI_CACHE_TTL
    with Session(engine) as session:
        if session.get(Analysis, analysis_id) is None:
            return error_response(404, "not_found", f"No analysis with id {analysis_id}.")
        rows = session.execute(
            select(SequenceRecord.id, SequenceRecord.header)
            .where(SequenceRecord.analysis_id == analysis_id)
            .order_by(SequenceRecord.id)
            .limit(MAX_NCBI_LOOKUPS)
        ).all()
        accession_of = {seq_id: extract_accession(header) for seq_id, header in rows}
        wanted = {accession for accession in accession_of.values() if accession}
        known: dict[str, Summary | None] = {
            row.accession: _cached_summary(row)
            for row in session.scalars(
                select(NcbiCache).where(
                    NcbiCache.accession.in_(wanted), NcbiCache.fetched_at > cutoff
                )
            )
        }

    # 2. Ask NCBI for the rest. No database connection is held meanwhile:
    #    the call can take seconds, and an idle pooled connection is wasted.
    missing = sorted(wanted - known.keys())
    ncbi_available = True
    if missing:
        try:
            fetched = fetch_summaries(missing)
        except NcbiUnavailable:
            logger.warning("NCBI unavailable", exc_info=True)
            ncbi_available = False
        else:
            # 3. Cache NCBI's answers, found or not. Failures are never cached.
            _store_summaries(fetched)
            known.update(fetched)

    return {
        "ncbi_available": ncbi_available,
        "records": {
            seq_id: _ncbi_record(accession, known[accession])
            for seq_id, accession in accession_of.items()
            if accession in known
        },
    }


def _cached_summary(row: NcbiCache) -> Summary | None:
    if row.accession_version is None:  # cached "not found"
        return None
    return Summary(row.accession_version, row.title, row.organism, row.length)


def _store_summaries(fetched: dict[str, Summary | None]) -> None:
    rows = [
        {"accession": accession, **asdict(summary)}
        if summary
        else {"accession": accession, "accession_version": None, "title": None,
              "organism": None, "length": None}
        for accession, summary in fetched.items()
    ]
    # Upsert: insert new accessions, refresh expired ones, in one statement.
    # Also safe when two requests look up the same accession concurrently.
    stmt = pg_insert(NcbiCache).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[NcbiCache.accession],
        set_={
            "fetched_at": func.now(),
            "accession_version": stmt.excluded.accession_version,
            "title": stmt.excluded.title,
            "organism": stmt.excluded.organism,
            "length": stmt.excluded.length,
        },
    )
    # Keep only the newest rows. NOT IN rather than a timestamp cutoff: rows
    # written by one upsert share fetched_at, so a cutoff could be off by a batch.
    newest = (
        select(NcbiCache.accession)
        .order_by(NcbiCache.fetched_at.desc())
        .limit(MAX_NCBI_CACHE_ROWS)
    )
    with Session(engine) as session, session.begin():
        session.execute(stmt)
        session.execute(delete(NcbiCache).where(NcbiCache.accession.not_in(newest)))


def _ncbi_record(accession: str, summary: Summary | None) -> NcbiRecord:
    if summary is None:
        return NcbiRecord(accession=accession, found=False)
    return NcbiRecord(
        accession=accession,
        found=True,
        **asdict(summary),
        # Built here from a fixed https prefix; the page never assembles URLs.
        url=f"https://www.ncbi.nlm.nih.gov/nuccore/{quote(summary.accession_version)}",
    )


# Local dev: serve the frontend from the same origin as the API, as Vercel
# does (its CDN serves public/ before requests reach this app). Must stay
# last: a mount at "/" matches everything, so routes registered after it
# would never be reached. Skipped if public/ isn't in the deployed bundle.
PUBLIC_DIR = FilePath(__file__).resolve().parent.parent / "public"
if PUBLIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
