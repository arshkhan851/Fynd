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

### Kaggle discovery
Install the API requirements to add the official Kaggle client, then configure Kaggle authentication with `apps\api\.venv\Scripts\kaggle.exe auth login` from the repository root or `.\.venv\Scripts\kaggle.exe auth login` from `apps\api`; alternatively configure `KAGGLE_API_TOKEN` (never commit credentials). To manually run the bounded Kaggle listing adapter and persist up to 50 candidates:

```powershell
cd apps\api
.\.venv\Scripts\python -m app.discovery.cli --kaggle --limit 50
```

Use `--limit` and `--page-size` to tune the bounded listing request. The command requires working Kaggle credentials and database connectivity; tests do not contact Kaggle.

### Manual evidence review
Discovery stores extracted values as candidate evidence; canonical competition fields are changed only when the evidence is accepted. From `apps\api`, list pending evidence, inspect one competition (including its source URLs and field evidence), or decide on an evidence record by ID:

```powershell
.\.venv\Scripts\python.exe -m app.review.cli list --limit 100
.\.venv\Scripts\python.exe -m app.review.cli show 123
.\.venv\Scripts\python.exe -m app.review.cli accept 456
.\.venv\Scripts\python.exe -m app.review.cli reject 789
```

Acceptance is supported for `title`, `description`, `eligibility_summary`, `format`, `event_timezone`, `official_url`, `registration_url`, `registration_opens`, `registration_deadline`, `event_starts`, and `event_ends`. Date evidence must provide either an ISO date in an `on` value or an offset-aware ISO datetime in an `at` value. Format, timezone, and URL evidence is validated before applying it. Organizer, category, topic, prize, and registration-status evidence can be inspected or rejected, but cannot be accepted until their canonical mapping is implemented. The CLI does not merge competitions.

The API loads the root `.env` file without overriding environment variables already set by the process. `DATABASE_URL` is the preferred connection setting; leave it blank to have the API safely build a PostgreSQL URL from `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, and `POSTGRES_DB`. A direct URL override must use URL-encoded credentials if they contain reserved characters.

## Known issues
- Plain `python` is 3.14 on this machine. Always use `py -3.11` for the venv.
