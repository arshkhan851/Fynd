# Setup

## PostgreSQL (Windows, PowerShell)
From the repository root, create `.env` from the example if you do not already have one. This preserves any existing local settings:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Review the `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` values in `.env` and set local development values if needed. The example values are for local development only.

Start PostgreSQL:

```powershell
docker compose up -d
```

Check the service and wait for `healthy` in the `STATUS` column:

```powershell
docker compose ps
```

Run the readiness check inside the container:

```powershell
docker compose exec db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Stop PostgreSQL while preserving its data:

```powershell
docker compose stop
```

Restart the stopped database:

```powershell
docker compose start
```

The database port is available only on `127.0.0.1:5432`; data persists in the named `fynd_postgres_data` volume. The root `.env` file is ignored by Git.

## API (Windows, PowerShell)
1. cd apps\api
2. py -3.11 -m venv .venv
3. .\.venv\Scripts\python -m pip install -r requirements.txt
4. Run database migrations from the API directory: .\.venv\Scripts\alembic upgrade head
5. Run: .\.venv\Scripts\python -m uvicorn app.main:app --reload
6. Open http://127.0.0.1:8000/health or http://127.0.0.1:8000/health/db
7. Test: .\.venv\Scripts\python -m pytest

The API loads the root `.env` file without overriding environment variables already set by the process. `DATABASE_URL` is the preferred connection setting; leave it blank to have the API safely build a PostgreSQL URL from `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, and `POSTGRES_DB`. A direct URL override must use URL-encoded credentials if they contain reserved characters.

## Known issues
- Plain `python` is 3.14 on this machine. Always use `py -3.11` for the venv.
