import os
from datetime import datetime

from sqlalchemy import DateTime, Engine, ForeignKey, String, Text, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import NullPool


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


class Base(DeclarativeBase):
    pass


class Analysis(Base):
    """One uploaded FASTA file."""

    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sequence_count: Mapped[int]
    total_length: Mapped[int]

    sequences: Mapped[list["SequenceRecord"]] = relationship(
        back_populates="analysis", order_by="SequenceRecord.id"
    )


class SequenceRecord(Base):
    """One record (header + sequence) inside an uploaded file."""

    __tablename__ = "sequences"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    header: Mapped[str] = mapped_column(String(1000))
    length: Mapped[int]
    gc_content: Mapped[float | None]
    count_a: Mapped[int]
    count_c: Mapped[int]
    count_g: Mapped[int]
    count_t: Mapped[int]
    count_other: Mapped[int]

    analysis: Mapped[Analysis] = relationship(back_populates="sequences")


class NcbiCache(Base):
    """One NCBI lookup, found or not. Rows older than the TTL are refetched."""

    __tablename__ = "ncbi_cache"

    accession: Mapped[str] = mapped_column(String(40), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # All four are None when NCBI has no such record (a cached "not found").
    accession_version: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(Text)
    organism: Mapped[str | None] = mapped_column(Text)
    length: Mapped[int | None]
