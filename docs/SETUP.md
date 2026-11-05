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
6. Open http://localhost:8000/health or http://localhost:8000/health/db
7. Test: .\.venv\Scripts\python -m pytest

### Competition listing API
`GET /competitions` returns competitions with a non-null canonical title, ordered by discovery time newest first and ID descending as a tie-breaker. It does not substitute pending evidence titles. The optional `q` parameter is trimmed and performs case-insensitive partial matching against canonical `title` and `description` columns only; empty and whitespace-only values behave like no search. The optional `category` parameter filters by an exact category slug. These filters can be combined. Candidate evidence is never searched. Pagination uses `limit` (default `20`, minimum `1`, maximum `100`) and `offset` (default `0`, minimum `0`). The response contains `items`, `total`, `limit`, and `offset`; `total` counts all matching competitions before pagination. Each item contains `id`, `title`, `format`, `official_url`, `event_starts_on`, `event_starts_at`, `registration_deadline_on`, and `registration_deadline_at`.

`GET /categories` returns categories ordered by name (case-insensitive), with the numeric `id`, `name`, `slug`, and `competition_count`. Counts include only competitions with a canonical non-null title, and categories with no eligible competitions are returned with a count of `0`.

`GET /competitions/{id}` returns `id`, `title`, `description`, `eligibility_summary`, `format`, official and registration URLs, registration/event dates, and `event_timezone`. Its public relationships are `categories` (`name`, `slug`, `is_primary`), `organizers` (`name`, `role`, `website_url`), `prizes` (`award_name`, `place`, decimal `amount` as a string, `currency`, `details`), and `sources` (`source_kind`, `source_url`, `publisher_name`, `publisher_homepage_url`). It returns `404` for missing competitions or records without a canonical title. Candidate, rejected, and superseded evidence is never returned or used as a fallback. External URLs are limited to credential-free HTTP(S) links. Source check status and verification timestamps are not exposed.

The web app requests the first page with `limit=20` from `API_BASE_URL`. Set `API_BASE_URL` in `apps\web\.env.local` to the API base URL; the local development value is:

```dotenv
API_BASE_URL=http://localhost:8000
```

### Kaggle discovery
Install the API requirements to add the official Kaggle client, then configure Kaggle authentication with `apps\api\.venv\Scripts\kaggle.exe auth login` from the repository root or `.\.venv\Scripts\kaggle.exe auth login` from `apps\api`; alternatively configure `KAGGLE_API_TOKEN` (never commit credentials). To manually run the bounded Kaggle listing adapter and persist up to 50 candidates:

```powershell
cd apps\api
.\.venv\Scripts\python -m app.discovery.cli --kaggle --limit 50
```

Use `--limit` and `--page-size` to tune the bounded listing request. The command requires working Kaggle credentials and database connectivity; tests do not contact Kaggle.

### Manual evidence review
Discovery stores extracted values as candidate evidence; canonical competition fields and category assignments change only when evidence is accepted. Seed the canonical category list idempotently, list pending category proposals, inspect a competition and its source IDs/evidence, or propose a category using an existing slug:

```powershell
.\.venv\Scripts\python.exe -m app.category_seed
.\.venv\Scripts\python.exe -m app.review.cli list --limit 100
.\.venv\Scripts\python.exe -m app.review.cli list --field category --limit 100
.\.venv\Scripts\python.exe -m app.review.cli show 123
.\.venv\Scripts\python.exe -m app.review.cli propose-category 123 ai-machine-learning --source-id 456
.\.venv\Scripts\python.exe -m app.review.cli accept 789
.\.venv\Scripts\python.exe -m app.review.cli reject 789
.\.venv\Scripts\python.exe -m app.review.cli unassign-category 123 ai-machine-learning
```

The category seed command reports newly created, already-existing, and conflicting categories; conflicts are left untouched. `propose-category` requires the competition ID, an existing category slug, and a source ID belonging to that competition. Add `--primary` to propose it as the primary category. Review proposals with `list --field category` or `show`, then use the normal `accept` or `reject` commands. Each accepted category is keyed by its canonical slug, and category relationships are not duplicated. A competition can have only one primary category; unassign the current primary explicitly before approving a different primary. `unassign-category` removes the assignment and supersedes its selected evidence. It does not delete the canonical category. Category, topic, prize, and registration-status proposals are not inferred or created automatically.

Acceptance remains supported for `title`, `description`, `eligibility_summary`, `format`, `event_timezone`, `official_url`, `registration_url`, `registration_opens`, `registration_deadline`, `event_starts`, and `event_ends`. Date evidence must provide either an ISO date in an `on` value or an offset-aware ISO datetime in an `at` value. Format, timezone, and URL evidence is validated before applying it. Organizer, topic, prize, and registration-status evidence can be inspected or rejected, but cannot be accepted until their canonical mapping is implemented. The CLI does not merge competitions.

The API loads the root `.env` file without overriding environment variables already set by the process. `DATABASE_URL` is the preferred connection setting; leave it blank to have the API safely build a PostgreSQL URL from `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, and `POSTGRES_DB`. A direct URL override must use URL-encoded credentials if they contain reserved characters.

## Known issues
- Plain `python` is 3.14 on this machine. Always use `py -3.11` for the venv.
