"""HTTP-layer tests through FastAPI's TestClient (in memory, no server).

The parser tests own the validation rules; these check how results and
errors reach the client. Tests using `db_client` need TEST_DATABASE_URL.
"""

import secrets
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db import Analysis, NcbiCache, engine
from api.index import MAX_ID, MAX_UPLOAD_BYTES
from api.ncbi import NcbiUnavailable, Summary

FIXTURES = Path(__file__).parent / "fixtures"


def upload(client, name, data=None):
    if data is None:
        data = (FIXTURES / name).read_bytes()
    return client.post("/api/analyze", files={"file": (name, data)})


# --- no database needed -------------------------------------------------------


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_missing_file_field_uses_our_error_shape(client):
    response = client.post("/api/analyze", data={"other": "x"})
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request", "message": "file: Field required"}


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("bad_character.fasta", "bad_character"),
        ("renamed_image.fasta", "not_text"),
        ("empty.fasta", "empty_file"),
    ],
)
def test_parser_errors_become_422_with_the_parser_code(client, name, code):
    response = upload(client, name)
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error", "message"}
    assert body["error"] == code


def test_one_byte_over_the_limit_is_413(client):
    response = upload(client, "big.fasta", b"A" * (MAX_UPLOAD_BYTES + 1))
    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"


def test_exactly_the_limit_passes_the_size_check(client):
    # Invalid FASTA on purpose, so no database is needed: getting the
    # parser's 422 instead of 413 proves the size check let it through.
    response = upload(client, "big.fasta", b"J" * MAX_UPLOAD_BYTES)
    assert response.status_code == 422
    assert response.json()["error"] == "not_fasta"


@pytest.mark.parametrize("analysis_id", ["0", "-1", str(MAX_ID + 1), "abc"])
def test_invalid_ids_are_rejected_before_any_query(client, analysis_id):
    response = client.get(f"/api/analyses/{analysis_id}")
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


def test_unreachable_database_is_503(client, monkeypatch):
    # Port 1 on localhost: nothing listens there, so connecting fails fast.
    unreachable = create_engine(
        "postgresql+psycopg://user:pass@127.0.0.1:1/db",
        poolclass=NullPool,
        connect_args={"connect_timeout": 2},
    )
    monkeypatch.setattr("api.index.engine", unreachable)
    response = client.get("/api/analyses")
    assert response.status_code == 503
    assert response.json()["error"] == "database_unavailable"


# --- database needed ----------------------------------------------------------


def test_upload_then_fetch_then_list(db_client):
    created = upload(db_client, "valid.fasta")
    assert created.status_code == 201
    analysis = created.json()
    assert analysis["filename"] == "valid.fasta"
    assert analysis["sequence_count"] == 3
    assert analysis["total_length"] == 73
    assert [s["gc_content"] for s in analysis["sequences"]] == pytest.approx([0.5, 0.5, 12 / 21])

    # Reading it back gives exactly what the upload returned.
    assert db_client.get(f"/api/analyses/{analysis['id']}").json() == analysis

    # Newest first, and list items are summaries without sequences.
    newest = db_client.get("/api/analyses").json()[0]
    assert newest["id"] == analysis["id"]
    assert "sequences" not in newest


def test_unknown_id_is_404(db_client):
    response = db_client.get(f"/api/analyses/{MAX_ID}")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "message": f"No analysis with id {MAX_ID}."}


def test_long_filename_is_trimmed_to_column_size(db_client):
    name = "x" * 300 + ".fasta"
    response = upload(db_client, name, (FIXTURES / "valid.fasta").read_bytes())
    assert response.status_code == 201
    assert response.json()["filename"] == name[:255]


# --- NCBI enrichment (database needed; NCBI itself is always faked) -------------


def fresh_accession() -> str:
    # Unique per run: the cache persists between runs, and fake results must
    # never be cached under a real accession.
    return f"TST{secrets.randbelow(10**9):09d}.1"


def fake_ncbi(monkeypatch, answer):
    """Replace the NCBI call; `answer` is a dict to return or an exception to raise."""
    calls = []

    def fetch_summaries(accessions):
        calls.append(accessions)
        if isinstance(answer, Exception):
            raise answer
        return {accession: answer.get(accession) for accession in accessions}

    monkeypatch.setattr("api.index.fetch_summaries", fetch_summaries)
    return calls


def test_ncbi_found_not_found_and_then_served_from_cache(db_client, monkeypatch):
    known, unknown = fresh_accession(), fresh_accession()
    fasta = f">{known} a\nACGT\n>{unknown} b\nACGT\n>plain_header c\nACGT\n".encode()
    analysis = upload(db_client, "ncbi.fasta", fasta).json()
    seq_known, seq_unknown, seq_plain = (str(s["id"]) for s in analysis["sequences"])
    summary = Summary(known, "Test record", "Testus organismus", 1234)

    calls = fake_ncbi(monkeypatch, {known: summary})
    first = db_client.get(f"/api/analyses/{analysis['id']}/ncbi").json()
    assert calls == [sorted([known, unknown])]  # one request for both
    assert first["ncbi_available"] is True
    assert first["records"][seq_known] == {
        "accession": known,
        "found": True,
        "accession_version": known,
        "title": "Test record",
        "organism": "Testus organismus",
        "length": 1234,
        "url": f"https://www.ncbi.nlm.nih.gov/nuccore/{known}",
    }
    assert first["records"][seq_unknown]["found"] is False
    assert seq_plain not in first["records"]  # no accession, nothing to look up

    # Both answers are cached, including "not found": NCBI must not be asked again.
    fake_ncbi(monkeypatch, AssertionError("NCBI called despite cache"))
    assert db_client.get(f"/api/analyses/{analysis['id']}/ncbi").json() == first


def test_ncbi_down_degrades_and_is_not_cached(db_client, monkeypatch):
    accession = fresh_accession()
    analysis = upload(db_client, "ncbi.fasta", f">{accession}\nACGT\n".encode()).json()
    url = f"/api/analyses/{analysis['id']}/ncbi"

    fake_ncbi(monkeypatch, NcbiUnavailable("simulated outage"))
    response = db_client.get(url)
    assert response.status_code == 200  # degraded, not failed
    assert response.json() == {"ncbi_available": False, "records": {}}

    # The failure wasn't cached: once NCBI is back, it is asked again.
    calls = fake_ncbi(monkeypatch, {accession: Summary(accession, "Back", "Online", 4)})
    assert db_client.get(url).json()["records"] != {}
    assert calls == [[accession]]


def test_ncbi_for_unknown_analysis_is_404(db_client):
    assert db_client.get(f"/api/analyses/{MAX_ID}/ncbi").status_code == 404


# --- storage caps (database needed) ---------------------------------------------
# Each test sets its cap to "exactly what exists now", so one more row must
# push out exactly one old row. On the shared dev branch that removes one old
# row per run, never more.


def stored(column):
    with Session(engine) as session:
        return session.scalars(select(column)).all()


def test_upload_past_the_cap_drops_only_the_oldest_analysis(db_client, monkeypatch):
    upload(db_client, "valid.fasta")  # at least one analysis exists to be dropped
    before = sorted(stored(Analysis.id))
    monkeypatch.setattr("api.index.MAX_STORED_ANALYSES", len(before))

    new_id = upload(db_client, "valid.fasta").json()["id"]

    after = sorted(stored(Analysis.id))
    assert len(after) == len(before)  # didn't grow
    assert before[0] not in after  # the oldest went
    assert after == before[1:] + [new_id]  # and nothing else did


def test_ncbi_cache_past_the_cap_keeps_the_newest(db_client, monkeypatch):
    seed, accession = fresh_accession(), fresh_accession()
    seeded = upload(db_client, "ncbi.fasta", f">{seed}\nACGT\n".encode()).json()
    fake_ncbi(monkeypatch, {})  # seed is "not found", which is cached too
    db_client.get(f"/api/analyses/{seeded['id']}/ncbi")  # so at least one row exists

    analysis = upload(db_client, "ncbi.fasta", f">{accession}\nACGT\n".encode()).json()
    before = len(stored(NcbiCache.accession))
    monkeypatch.setattr("api.index.MAX_NCBI_CACHE_ROWS", before)

    fake_ncbi(monkeypatch, {accession: None})
    db_client.get(f"/api/analyses/{analysis['id']}/ncbi")

    after = stored(NcbiCache.accession)
    assert len(after) == before  # didn't grow
    assert accession in after  # the newest row is kept
