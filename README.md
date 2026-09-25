# sequence-api

FastAPI service deployed on Vercel, backed by Postgres on Neon.

Production: https://sequence-api-peach.vercel.app

## Endpoints

| Method | Path            | Response                                                        |
| ------ | --------------- | --------------------------------------------------------------- |
| GET    | `/api/health`   | `{"status": "ok"}` — never touches the database                 |
| GET    | `/api/db-check` | `{"status": "ok", "result": 1}` after `SELECT 1`, or `503` with `{"status": "error", "error": "<reason>"}` |

## Local development

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 (from `.python-version`) if needed.

```sh
uv venv
uv pip install -r requirements.txt uvicorn
```

Create `.env` (gitignored) with your Neon connection string:

```
DATABASE_URL=postgresql://USER:PASSWORD@HOST-pooler.REGION.aws.neon.tech/DB?sslmode=require
```

Run:

```sh
uv run --env-file .env uvicorn api.index:app --reload
```

Then open http://127.0.0.1:8000/api/health and http://127.0.0.1:8000/api/db-check.

## Deployment

Vercel detects FastAPI automatically from `api/index.py` (the `app` instance) and installs `requirements.txt`. There is no `vercel.json`, by design.

Set `DATABASE_URL` in the Vercel project's environment variables (the Neon integration does this automatically). Use Neon's **pooled** connection string (host contains `-pooler`).

## Design notes

- **`NullPool`**: serverless function instances don't persist between invocations, so an in-process SQLAlchemy pool would hold connections nothing reuses. Every request opens and closes a real connection; Neon's pooler (PgBouncer) does the pooling.
- **psycopg 3**: the app rewrites `postgresql://` URLs to `postgresql+psycopg://`, because SQLAlchemy otherwise defaults to psycopg2, which isn't installed.
- **`connect_timeout=10`**: an unreachable database fails with a `503` instead of hanging until Vercel kills the function.
- **Sync endpoints (`def`)**: FastAPI runs them in a threadpool, so blocking database calls don't stall the event loop.
