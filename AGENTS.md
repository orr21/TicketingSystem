# AGENTS.md

Databricks App (Streamlit) + Lakebase (managed PostgreSQL) support-ticket kanban. Everything lives in `app.py`; `schema.sql` mirrors the runtime schema.

## Run / verify

- **Local (recommended): no Databricks needed.** `make install && make db-up && make run` → http://localhost:8501. Runs against a Docker Postgres on host port **5433** (5432 is often taken). `make test` runs the AppTest smoke test (`tests/smoke_test.py`) against the local DB. `make db-reset` drops all tables so the app re-provisions + re-seeds.
- **Lakebase**: `databricks auth login`, `pip install -r requirements.txt`, set `PGHOST/PGPORT/PGDATABASE/PGUSER/PGSSLMODE=require` + `ENDPOINT_NAME`, then `streamlit run app.py`. Without a real Lakebase endpoint + OAuth role it cannot run.
- Deployed via `app.yaml` (Databricks Apps). Never construct the DB hostname from the workspace host — use the injected `PGHOST`.
- There is no CI or linter. Verification = `make test` + launching locally.

## Architecture gotchas

- All DB/auth/UI logic is in one file. `WorkspaceClient()` is initialized lazily and wrapped in try/except (`_init_workspace`) — it returns `None` when Databricks auth is unavailable, which is fine because it's only used in Lakebase mode. Expect everything to run on every Streamlit rerun.
- **Dev mode**: when `LAKEBASE_ENDPOINT`/`ENDPOINT_NAME` are unset, the app connects with plain `PGPASSWORD` (no OAuth token) and `get_current_user()` returns `DEV_USER` (default `dev@example.com`) instead of calling Databricks. `.env` is loaded via `load_dotenv()` at import. Never gate Lakebase-only code on `w` being non-None — use `_is_lakebase()`.
- Connection/token: a fresh OAuth token is minted per connection via `w.postgres.generate_database_credential(endpoint=...)` and used as the Postgres password. Connections are recycled every 900s (tokens expire after 1h). `LAKEBASE_ENDPOINT` (from the bound resource) or `ENDPOINT_NAME` env is required.
- Schema is auto-provisioned at runtime (`ensure_schema`, app.py:30). **Keep `app.py` DDL in sync with `schema.sql`** — both must change together. There are three tables: `tickets`, `ticket_messages`, `app_meta`.
- Sample data is seeded **only once**, gated by the `seed_version` key in `app_meta` — never gate seeding on "tickets table is empty" (that would re-seed after a user deletes everything).
- The `priority` column is a post-create migration with a fallback: if the app SP is not the table owner, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` fails and it instructs the user to run the migration manually in the SQL Editor (app.py:43). Adding new columns must follow this tolerant pattern or you'll break the non-owner case. CHECK constraints are also re-applied best-effort in a try/except so shared deployments degrade gracefully.
- All queries use `%s` parameters (psycopg 3). **Close every transaction**: reads must `conn.commit()` too (not just writes) or the connection stays "idle in transaction" holding an ACCESS SHARE lock, which blocks `ensure_schema`'s `ALTER TABLE` and makes new connections hang. Writes wrap in try/except with `conn.rollback()`.

## Invariants

- Statuses: `open`, `in_progress`, `resolved`. Priorities: `low`, `medium`, `high`, `urgent`. The canonical lists are the `VALID_STATUSES`/`VALID_PRIORITIES` constants in app.py — use those everywhere (they also back the UI selectboxes). The same values are hardcoded in `COLUMN_STATUS`, `PRIORITY_COLORS`, `schema.sql`, and app.py sample data.
- Kanban drag-and-drop (`streamlit-dnd`) requires stable `st.container(key=...)` keys — `col_*` for columns, `ticket_<id>` for cards. Drop events are deduped via the `last_drop` session-state signature; keep that guard if you touch the DnD handler.
- The ticket modal is driven by `st.session_state.selected_ticket` (set on "View Details", cleared on close). After any status/priority update the code clears it and calls `st.rerun()`.
- Author names come from `get_current_user()` (Databricks identity in Lakebase mode, `DEV_USER` locally) for created_by/messages.
- README.md has setup + troubleshooting (OAuth role creation, "password authentication failed" fixes); schema.sql documents the manual role grants.
