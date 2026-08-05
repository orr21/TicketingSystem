# Ticketing System

A support-ticketing app built as a **Databricks App** (Streamlit) with a **Lakebase** managed PostgreSQL backend. Users create support tickets, add messages, and move tickets across an open / in-progress / resolved kanban board. Built as the Day 1 homework of a Databricks bootcamp, and designed to become the data foundation for the context-engineering and AI-agent projects that follow.

## Features

- Kanban board with **drag-and-drop** status changes (no modal, no move buttons)
- Open a ticket to view details and full message history in a modal
- Create new tickets
- Add messages to an existing ticket
- Change a ticket's status (drag on the board, or in the ticket modal)
- Schema and sample data **auto-provisioned**: tables are created and seeded on first run if they don't exist
- OAuth service-principal authentication via the Databricks SDK — no credentials hard-coded or committed

## Tech stack

- **Databricks Apps** — serverless hosting, managed identity for the app
- **Streamlit** + `streamlit-dnd` — UI and drag-and-drop kanban
- **Lakebase** (managed PostgreSQL, Autoscaling) — operational storage
- **databricks-sdk** + **psycopg 3** — OAuth credentials and Postgres connectivity

## Data model

Two related tables in the `databricks_postgres` database:

```sql
tickets (
  ticket_id   SERIAL PRIMARY KEY,
  title       TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open',   -- open | in_progress | resolved
  created_by  TEXT,
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)

ticket_messages (
  message_id   SERIAL PRIMARY KEY,
  ticket_id    INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
  message_text TEXT NOT NULL,
  author       TEXT,
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

Sample data (3 tickets, 2 messages each, across all three statuses) is seeded by the app on first run, or manually via `schema.sql` in the Lakebase SQL Editor.

## How the app connects

- The app runs as its own **service principal**; the Databricks Apps runtime injects `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGSSLMODE`.
- `LAKEBASE_ENDPOINT` is resolved from the bound database resource via `valueFrom: postgres` in `app.yaml`.
- On every connection the app mints a fresh OAuth database credential with `w.postgres.generate_database_credential(endpoint=LAKEBASE_ENDPOINT)` and uses the token as the Postgres password.
- Tokens expire after 1 hour; the app reconnects with a fresh token every 15 minutes.

Do **not** construct the database hostname from the workspace host — use the injected `PGHOST` (e.g. `ep-<id>.database.<region>.cloud.databricks.com`).

## Project layout

```
app.py            Streamlit application (UI + DB access)
app.yaml          Databricks Apps configuration (command, env, resources)
schema.sql        Lakebase DDL + sample data, runnable in the Lakebase SQL Editor
requirements.txt  Python dependencies
docker-compose.yml  Local Postgres for dev (host port 5433)
Makefile          Dev workflow: install / db-up / db-down / db-reset / run / test
tests/            AppTest smoke test
.streamlit/       Streamlit theme + server config
```

## Local development

### Quick start (Docker + local Postgres — no Databricks/Lakebase needed)

The app ships with a "dev mode": if `LAKEBASE_ENDPOINT`/`ENDPOINT_NAME` are not set,
it connects to a plain Postgres using `PGPASSWORD` and uses `DEV_USER` as the author —
no OAuth, no Databricks auth required.

```bash
make install        # create .venv + install dependencies
make db-up          # start a local Postgres container (port 5433)
make run            # start the app at http://localhost:8501 (creates .env from .env.example)
```

- `make test` runs a smoke test (renders the app against the local DB and creates/deletes a ticket).
- `make db-reset` drops all tables so the app re-provisions + re-seeds on next run.
- `make db-down` / `make clean` stop the container / remove everything.
- Config lives in `.env` (gitignored); defaults are in `.env.example`. The DB is
  seeded with sample data on first connect.

### Against real Lakebase

```bash
databricks auth login
pip install -r requirements.txt

export PGHOST="<endpoint host>"
export PGPORT="5432"
export PGDATABASE="databricks_postgres"
export PGUSER="you@example.com"          # your own identity, not the app SP
export PGSSLMODE="require"
export ENDPOINT_NAME="projects/<id>/branches/<id>/endpoints/<id>"

streamlit run app.py
```

For local runs the connection is authenticated as *you*, so an OAuth role for your own Databricks identity must exist in the database.

## Deploy to Databricks Apps

1. Create a Databricks app in the workspace.
2. In **App resources**, add the Lakebase database resource: project → branch (`production`) → database (`databricks_postgres`), permission **Can connect and create**, resource key **`postgres`**. This injects the `PG*` connection variables and creates the app's Postgres role.
3. Deploy the code from this repo / workspace folder.
4. Open the app URL.

### One-time database setup (if auto-provisioning fails)

Run `schema.sql` in the Lakebase SQL Editor, and make sure the app's service principal has an **OAuth** role:

```sql
CREATE EXTENSION IF NOT EXISTS databricks_auth;
SELECT databricks_create_role('<app-service-principal-client-id>', 'SERVICE_PRINCIPAL');
GRANT CONNECT ON DATABASE databricks_postgres TO "<app-service-principal-client-id>";
GRANT USAGE, CREATE ON SCHEMA public TO "<app-service-principal-client-id>";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<app-service-principal-client-id>";
```

## Troubleshooting

- **`PGHOST/PGUSER not found in environment`** — the Lakebase database resource is not bound to the app (Apps → App resources → Database, key `postgres`).
- **`password authentication failed`** — the app SP role exists but is *not* an OAuth role (or is `No login`). Drop it via **Roles & Databases → Drop** (enable *Reassign owned objects*) and recreate it with `databricks_create_role(..., 'SERVICE_PRINCIPAL')`.
- **Wrong host / connection refused** — someone is building the hostname manually. Use the injected `PGHOST` and the resolved `LAKEBASE_ENDPOINT`.

## Next steps

- **Enable Lakebase CDF (Change Data Feed)** — a Public Preview feature that streams every insert/update/delete on `tickets` and `ticket_messages` into Unity Catalog managed tables with native CDC, no connectors or replication sidecars. This makes the operational data directly queryable by Databricks SQL, downstream pipelines, and — most importantly — the AI agents coming later in the bootcamp.
- **AI-agent integration** — give a Genie space or custom agent access to the CDF tables (or the Lakebase Data API) so it can answer support questions against live data.
- **Bootcamp bonus features** — ticket priority/category, filtering by status, input validation, ticket statistics, delete-with-confirmation, visual polish.
- **CI/CD** — version-control the schema and app, and automate deploys (Databricks bundles; CDF pipeline components for the sync job).
