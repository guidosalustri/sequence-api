"""HTTP-layer tests through FastAPI's TestClient (in memory, no server).

The parser tests own the validation rules; these check how results and
errors reach the client. Tests using `db_client` need TEST_DATABASE_URL.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from api.index import MAX_ID, MAX_UPLOAD_BYTES

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
