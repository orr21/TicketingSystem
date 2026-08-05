"""Smoke test for the Ticketing System app.

Renders the whole app with Streamlit's AppTest against the local dev DB
(docker-compose.yml) and exercises the end-to-end "create ticket" flow.

Usage:  make test   (requires: make install, make db-up)
"""

import os
from pathlib import Path
import psycopg
from streamlit.testing.v1 import AppTest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _db_conn():
    return psycopg.connect(
        host=os.environ.get('PGHOST', 'localhost'),
        port=os.environ.get('PGPORT', '5433'),
        dbname=os.environ.get('PGDATABASE', 'ticketing'),
        user=os.environ.get('PGUSER', 'postgres'),
        password=os.environ.get('PGPASSWORD', 'postgres'),
        sslmode=os.environ.get('PGSSLMODE', 'disable'),
    )


def main():
    at = AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=30)
    at.run()

    if at.exception:
        for exc in at.exception:
            print("EXCEPTION:", exc.value)
        raise SystemExit("App raised exceptions on render")

    assert at.button(key="new_ticket_btn"), "Sidebar New Ticket button missing"
    print("Base render OK: sidebar, metrics, kanban board")

    # End-to-end: open the New Ticket dialog and create a ticket
    at.button(key="new_ticket_btn").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.get("dialog")) == 1, "New Ticket dialog did not open"

    at.text_input(key="new_ticket_title").set_value("Smoke test ticket")
    at.button(key="create_ticket").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.get("dialog")) == 0, "Dialog did not close after create"
    print("Ticket creation flow OK")

    # Clean up the smoke-test ticket so the dev DB stays tidy
    with _db_conn() as conn:
        conn.execute("DELETE FROM tickets WHERE title = 'Smoke test ticket'")
    print("Cleanup OK")

    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
