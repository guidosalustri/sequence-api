"""Response shapes for the API.

Kept separate from the database models so the table layout and the public
contract can change independently. from_attributes lets FastAPI build these
directly from SQLAlchemy objects.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SequenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    header: str
    length: int
    gc_content: float | None
    count_a: int
    count_c: int
    count_g: int
    count_t: int
    count_other: int


class AnalysisSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    uploaded_at: datetime
    sequence_count: int
    total_length: int


class AnalysisDetail(AnalysisSummary):
    sequences: list[SequenceOut]


class NcbiRecord(BaseModel):
    accession: str
    found: bool
    accession_version: str | None = None
    title: str | None = None
    organism: str | None = None
    length: int | None = None
    url: str | None = None


class NcbiEnrichment(BaseModel):
    # False when NCBI couldn't be reached; records then hold cached results only.
    ncbi_available: bool
    # Keyed by sequence id. Records without an accession are absent.
    records: dict[int, NcbiRecord]
