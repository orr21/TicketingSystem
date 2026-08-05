# AGENTS.md

Databricks App (Streamlit) + Lakebase (managed PostgreSQL) support-ticket kanban. Everything lives in `app.py`; `schema.sql` mirrors the runtime schema. There is no test suite, linter, or CI — the only verification is launching the app locally.

## Run / verify

- Local: `databricks auth login`, `pip install -r requirements.txt`, then set `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` (your own identity), `PGSSLMODE=require`, and `ENDPOINT_NAME` before `streamlit run app.py`. Without a real Lakebase endpoint and DB role it cannot run.
- Deployed via `app.yaml` (Databricks Apps). Never construct the DB hostname from the workspace host — use the injected `PGHOST`.

## Architecture gotchas

- All DB/auth/UI logic is in one file. `WorkspaceClient()` and `ensure_schema()` run at import/module level — expect everything to run on every Streamlit rerun.
- Connection/token: a fresh OAuth token is minted per connection via `w.postgres.generate_database_credential(endpoint=...)` and used as the Postgres password. Connections are recycled every 900s (tokens expire after 1h). `LAKEBASE_ENDPOINT` (from the bound resource) or `ENDPOINT_NAME` env is required.
- Schema is auto-provisioned at runtime (`ensure_schema`, app.py:30) and seeded when `tickets` is empty. **Keep `app.py` DDL in sync with `schema.sql`** — both must change together.
- The `priority` column is a post-create migration with a fallback: if the app SP is not the table owner, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` fails and it instructs the user to run the migration manually in the SQL Editor (app.py:43). Adding new columns must follow this tolerant pattern or you'll break the non-owner case.
- All queries use `%s` parameters (psycopg 3). Use `RETURNING` + `conn.commit()`; every write wraps in try/except with `conn.rollback()`.

## Invariants

- Statuses: `open`, `in_progress`, `resolved`. Priorities: `low`, `medium`, `high`, `urgent`. These are hardcoded in several places (selectboxes, `COLUMN_STATUS` map, `PRIORITY_COLORS`, `schema.sql`, app.py sample data). Changing them means touching all spots.
- Kanban drag-and-drop (`streamlit-dnd`) requires stable `st.container(key=...)` keys — `col_*` for columns, `ticket_<id>` for cards. Drop events are deduped via the `last_drop` session-state signature; keep that guard if you touch the DnD handler.
- The ticket modal is driven by `st.session_state.selected_ticket` (set on "View Details", cleared on close). After any status/priority update the code clears it and calls `st.rerun()`.
- Author names come from `w.current_user.me().user_name` for created_by/messages.
- README.md has setup + troubleshooting (OAuth role creation, "password authentication failed" fixes); schema.sql documents the manual role grants.
