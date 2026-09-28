"""NCBI client tests. Never calls NCBI: responses are real ones saved as
fixtures, and network failures are simulated by replacing urlopen."""

import io
import json
import urllib.error
from pathlib import Path

import pytest

from api.ncbi import NcbiUnavailable, Summary, extract_accession, fetch_summaries, match_summaries

FIXTURES = Path(__file__).parent / "fixtures"
TP53 = Summary(
    "NM_000546.6", "Homo sapiens tumor protein p53 (TP53), transcript variant 1, mRNA", "Homo sapiens", 2512
)


def saved_response(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- extract_accession ----------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("NM_000546.6 Homo sapiens tumor protein p53", "NM_000546.6"),  # RefSeq, versioned
        ("MN908947.3 SARS-CoV-2 Wuhan-Hu-1", "MN908947.3"),  # GenBank
        ("NM_000546", "NM_000546"),  # unversioned
        ("nm_000546.6 lowercase", "NM_000546.6"),  # normalized: NCBI answers uppercase
        ("seq1 test record", None),
        ("sample07_contig_12 len=1532", None),
        ("gi|224589800|ref|NC_000001.10| chr1", None),  # retired pipe style: not supported
    ],
)
def test_extract_accession(header, expected):
    assert extract_accession(header) == expected


# --- match_summaries (real saved responses) --------------------------------------


def test_match_versioned_unversioned_and_unknown():
    requested = ["NM_000546.6", "MN908947.3", "NM_000546", "ZZ999999.9"]
    assert match_summaries(requested, saved_response("ncbi_esummary_mixed.json")) == {
        "NM_000546.6": TP53,
        "MN908947.3": Summary(
            "MN908947.3",
            "Severe acute respiratory syndrome coronavirus 2 isolate Wuhan-Hu-1, complete genome",
            "Severe acute respiratory syndrome coronavirus 2",
            29903,
        ),
        "NM_000546": TP53,  # unversioned: NCBI resolves to the latest version
        "ZZ999999.9": None,  # NCBI answered 200 but returned nothing for it
    }


def test_match_when_every_accession_is_unknown():
    requested = ["ZZ999999.9", "ZZ999998.9"]
    assert match_summaries(requested, saved_response("ncbi_esummary_all_invalid.json")) == {
        "ZZ999999.9": None,
        "ZZ999998.9": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},  # no "result" at all
        {"result": []},  # wrong type
        {"result": {"uids": ["1"]}},  # uid listed but no document for it
    ],
)
def test_unexpected_response_shape_is_unavailable_not_a_crash(payload):
    with pytest.raises(NcbiUnavailable):
        match_summaries(["NM_000546.6"], payload)


# --- fetch_summaries (network simulated) -----------------------------------------


def fake_urlopen(monkeypatch, *, body=None, error=None):
    """Replace urlopen; returns the list of URLs it was called with."""
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        if error:
            raise error
        return io.BytesIO(body)  # BytesIO works as a context manager, like a response

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("timed out"),
        urllib.error.HTTPError("https://eutils", 429, "Too Many Requests", {}, None),
        urllib.error.URLError("Name or service not known"),
    ],
)
def test_network_failures_become_unavailable(monkeypatch, error):
    fake_urlopen(monkeypatch, error=error)
    with pytest.raises(NcbiUnavailable):
        fetch_summaries(["NM_000546.6"])


def test_non_json_body_becomes_unavailable(monkeypatch):
    fake_urlopen(monkeypatch, body=b"<html>Service Unavailable</html>")
    with pytest.raises(NcbiUnavailable):
        fetch_summaries(["NM_000546.6"])


def test_one_request_for_all_accessions(monkeypatch):
    calls = fake_urlopen(monkeypatch, body=(FIXTURES / "ncbi_esummary_mixed.json").read_bytes())
    result = fetch_summaries(["NM_000546.6", "ZZ999999.9"])
    assert result == {"NM_000546.6": TP53, "ZZ999999.9": None}
    assert len(calls) == 1
    assert "id=NM_000546.6%2CZZ999999.9" in calls[0]
    assert "tool=sequence-api" in calls[0]
