"""Create any missing tables in the database DATABASE_URL points at.

Run once per database, from the repo root:
    uv run --env-file .env python -m scripts.create_tables

create_all() skips tables that already exist and never alters them.
"""

from api.db import Base, engine

if engine is None:
    raise SystemExit("DATABASE_URL is not set")

Base.metadata.create_all(engine)
print("Done: tables created (existing ones left untouched).")
