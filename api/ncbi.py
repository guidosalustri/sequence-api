"""Look up accession numbers in NCBI's public E-utilities (esummary, nuccore).

NCBI is someone else's service: sometimes slow, sometimes down, occasionally
rate-limiting us. Every failure surfaces as NcbiUnavailable so the caller can
degrade to local stats instead of erroring. No FastAPI or database code here.
"""

import http.client
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
TOOL = "sequence-api"  # NCBI asks callers to identify themselves
# Applies to each network operation (connect, each read), not the whole request.
TIMEOUT_SECONDS = 5

# Loose on purpose: NCBI is the authority, and a false match only costs one
# cached "not found". Letters, optional "_" (RefSeq), digits, optional version.
ACCESSION = re.compile(r"[A-Z]{1,6}_?\d{5,12}(\.\d+)?", re.IGNORECASE)


class NcbiUnavailable(Exception):
    pass


@dataclass
class Summary:
    accession_version: str
    title: str
    organism: str
    length: int


def extract_accession(header: str) -> str | None:
    """The header's first word, uppercased, if it looks like an accession."""
    first_word = header.split(maxsplit=1)[0]
    # NCBI answers in uppercase, so matching needs one canonical form.
    return first_word.upper() if ACCESSION.fullmatch(first_word) else None


def fetch_summaries(accessions: list[str]) -> dict[str, Summary | None]:
    """One esummary request for all accessions. None = NCBI has no such record."""
    query = urllib.parse.urlencode(
        {"db": "nuccore", "id": ",".join(accessions), "retmode": "json", "tool": TOOL}
    )
    try:
        with urllib.request.urlopen(f"{ESUMMARY_URL}?{query}", timeout=TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # OSError: network, DNS, timeouts, HTTP errors (429, 5xx).
        # HTTPException: connection dropped mid-response. ValueError: not JSON.
        raise NcbiUnavailable(str(exc)) from exc
    return match_summaries(accessions, payload)


def match_summaries(requested: list[str], payload: dict) -> dict[str, Summary | None]:
    """Map NCBI's results back to what we asked for.

    NCBI keys results by its internal uid, not by our input, and answers 200
    even for accessions it doesn't know. So: versioned input matches
    accessionversion ("NM_000546.6"), unversioned input matches caption
    ("NM_000546", which NCBI resolves to the latest version), and anything
    not returned is not found.
    """
    try:
        result = payload["result"]
        by_version: dict[str, Summary] = {}
        by_caption: dict[str, Summary] = {}
        for uid in result["uids"]:
            doc = result[uid]
            summary = Summary(doc["accessionversion"], doc["title"], doc["organism"], doc["slen"])
            by_version[doc["accessionversion"]] = summary
            by_caption[doc["caption"]] = summary
    except (KeyError, TypeError) as exc:
        raise NcbiUnavailable(f"unexpected response shape: {exc!r}") from exc
    return {
        accession: (by_version if "." in accession else by_caption).get(accession)
        for accession in requested
    }
