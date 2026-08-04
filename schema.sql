-- TicketingSystem: Lakebase schema and sample data
-- Run this in the Lakebase SQL Editor (database: databricks_postgres).
-- The app also auto-creates these tables and seeds them if they don't exist.

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_by TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    message_id SERIAL PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
    message_text TEXT NOT NULL,
    author TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

WITH new_tickets AS (
    INSERT INTO tickets (title, status, created_by)
    VALUES
        ('Cannot log in to VPN', 'open', 'alice@example.com'),
        ('Billing discrepancy on invoice #2041', 'in_progress', 'bob@example.com'),
        ('Databricks app deploy failing', 'resolved', 'carol@example.com')
    ON CONFLICT DO NOTHING
    RETURNING ticket_id, title
)
INSERT INTO ticket_messages (ticket_id, message_text, author)
SELECT t.ticket_id, m.message_text, m.author
FROM new_tickets t
JOIN (VALUES
    ('Cannot log in to VPN', 'Getting a timeout when connecting to VPN.', 'alice@example.com'),
    ('Cannot log in to VPN', 'We are investigating, please share your client logs.', 'support@example.com'),
    ('Billing discrepancy on invoice #2041', 'Invoice shows an extra charge for last month.', 'bob@example.com'),
    ('Billing discrepancy on invoice #2041', 'Confirmed the overcharge, a refund is being processed.', 'support@example.com'),
    ('Databricks app deploy failing', 'Deploy fails with a missing module error.', 'carol@example.com'),
    ('Databricks app deploy failing', 'Fixed by pinning the dependency version.', 'support@example.com')
) AS m(title, message_text, author) ON m.title = t.title;

-- Optional: grant the app service principal access if the role was not auto-provisioned.
-- Replace <DATABRICKS_CLIENT_ID> with the client ID shown in the app's Environment tab.
-- GRANT CONNECT ON DATABASE databricks_postgres TO "<DATABRICKS_CLIENT_ID>";
-- GRANT USAGE, CREATE ON SCHEMA public TO "<DATABRICKS_CLIENT_ID>";
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "<DATABRICKS_CLIENT_ID>";
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "<DATABRICKS_CLIENT_ID>";
