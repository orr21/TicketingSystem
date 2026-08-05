import streamlit as st
import psycopg
import os
import time
import html
from databricks.sdk import WorkspaceClient
from datetime import datetime, timezone
import pandas as pd
from streamlit_dnd import dnd
from dotenv import load_dotenv

# Load local .env overrides (no-op in the Databricks Apps runtime)
load_dotenv()

# Page configuration
st.set_page_config(page_title="Ticketing System", page_icon="🎫", layout="wide", initial_sidebar_state="expanded")

def _is_lakebase():
    """True when running against Lakebase (OAuth token auth) vs a local Postgres"""
    return bool(os.environ.get('LAKEBASE_ENDPOINT') or os.environ.get('ENDPOINT_NAME'))

def _init_workspace():
    """Create the Databricks WorkspaceClient lazily; returns None when not available
    (e.g. local dev without Databricks auth)."""
    try:
        return WorkspaceClient()
    except Exception:
        return None

# Initialize Databricks client (only needed for Lakebase mode)
w = _init_workspace()

# Global connection state
if 'conn' not in st.session_state or 'last_token_refresh' not in st.session_state:
    st.session_state.last_token_refresh = 0
    st.session_state.conn = None

def get_oauth_token():
    """Get a fresh Lakebase OAuth token for the bound database endpoint"""
    endpoint = os.environ.get('LAKEBASE_ENDPOINT') or os.environ.get('ENDPOINT_NAME')
    if not endpoint:
        raise ValueError("LAKEBASE_ENDPOINT not found in environment")

    if w is None:
        raise RuntimeError("WorkspaceClient is not available; cannot mint an OAuth database credential")

    credential = w.postgres.generate_database_credential(endpoint=endpoint)
    return credential.token

def ensure_schema(conn):
    """Create the tickets/ticket_messages tables and seed sample data if empty"""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'medium',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT tickets_status_check CHECK (status IN ('open', 'in_progress', 'resolved')),
                CONSTRAINT tickets_priority_check CHECK (priority IN ('low', 'medium', 'high', 'urgent'))
            )
        """)
        try:
            cur.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'medium'")
        except Exception:
            conn.rollback()
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'tickets' AND column_name = 'priority'"
            )
            if cur.fetchone()[0] == 0:
                raise RuntimeError(
                    "Cannot add the 'priority' column to the tickets table because the app is "
                    "not the table owner. Run this in the Lakebase SQL Editor as the owner: "
                    "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'medium';"
                )
        try:
            cur.execute(
                "ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_status_check;"
                "ALTER TABLE tickets ADD CONSTRAINT tickets_status_check "
                "CHECK (status IN ('open', 'in_progress', 'resolved'));"
                "ALTER TABLE tickets DROP CONSTRAINT IF EXISTS tickets_priority_check;"
                "ALTER TABLE tickets ADD CONSTRAINT tickets_priority_check "
                "CHECK (priority IN ('low', 'medium', 'high', 'urgent'));"
            )
            conn.commit()
        except Exception:
            conn.rollback()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                message_id SERIAL PRIMARY KEY,
                ticket_id INTEGER NOT NULL REFERENCES tickets(ticket_id) ON DELETE CASCADE,
                message_text TEXT NOT NULL,
                author TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("SELECT value FROM app_meta WHERE key = 'seed_version'")
        already_seeded = cur.fetchone() is not None
        if not already_seeded:
            # Seed atomically: drop autocommit for a single real transaction so a
            # partial failure can't leave duplicate sample data behind.
            conn.autocommit = False
            try:
                cur.execute("SELECT COUNT(*) FROM tickets")
                if cur.fetchone()[0] == 0:
                    sample_tickets = [
                        ('Cannot log in to VPN', 'open', 'high', 'alice@example.com'),
                        ('Billing discrepancy on invoice #2041', 'in_progress', 'medium', 'bob@example.com'),
                        ('Databricks app deploy failing', 'resolved', 'low', 'carol@example.com'),
                    ]
                    ticket_ids = []
                    for title, status, priority, created_by in sample_tickets:
                        cur.execute(
                            "INSERT INTO tickets (title, status, priority, created_by) VALUES (%s, %s, %s, %s) RETURNING ticket_id",
                            (title, status, priority, created_by),
                        )
                        ticket_ids.append(cur.fetchone()[0])
                    sample_messages = [
                        (ticket_ids[0], 'Getting a timeout when connecting to VPN.', 'alice@example.com'),
                        (ticket_ids[0], 'We are investigating, please share your client logs.', 'support@example.com'),
                        (ticket_ids[1], 'Invoice shows an extra charge for last month.', 'bob@example.com'),
                        (ticket_ids[1], 'Confirmed the overcharge, a refund is being processed.', 'support@example.com'),
                        (ticket_ids[2], 'Deploy fails with a missing module error.', 'carol@example.com'),
                        (ticket_ids[2], 'Fixed by pinning the dependency version.', 'support@example.com'),
                    ]
                    for ticket_id, message_text, author in sample_messages:
                        cur.execute(
                            "INSERT INTO ticket_messages (ticket_id, message_text, author) VALUES (%s, %s, %s)",
                            (ticket_id, message_text, author),
                        )
                cur.execute(
                    "INSERT INTO app_meta (key, value) VALUES ('seed_version', '1') "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        else:
            conn.commit()

def get_db_connection():
    """Get database connection with automatic token refresh"""
    current_time = time.time()

    # Refresh connection/token every 15 minutes (900 seconds)
    if st.session_state.conn is None or (current_time - st.session_state.last_token_refresh) > 900:
        try:
            # Close old connection if exists
            if st.session_state.conn:
                st.session_state.conn.close()

            # Use connection details injected by the Databricks Apps runtime
            # (or set in .env for local dev)
            host = os.environ.get('PGHOST')
            port = os.environ.get('PGPORT', '5432')
            dbname = os.environ.get('PGDATABASE', 'databricks_postgres')
            db_user = os.environ.get('PGUSER')
            sslmode = os.environ.get('PGSSLMODE', 'require')

            if not all([host, db_user]):
                raise ValueError("PGHOST/PGUSER not found in environment")

            # Password: OAuth token for Lakebase, plain PGPASSWORD for a local Postgres
            if _is_lakebase():
                password = get_oauth_token()
            else:
                password = os.environ.get('PGPASSWORD', '')

            # Create new connection.
            # autocommit=True so no transaction is ever left open — this prevents
            # "idle in transaction" connections from holding locks that block
            # ensure_schema's DDL on the next reconnect.
            st.session_state.conn = psycopg.connect(
                host=host,
                dbname=dbname,
                user=db_user,
                port=port,
                password=password,
                sslmode=sslmode,
                connect_timeout=10,
                autocommit=True
            )
            ensure_schema(st.session_state.conn)
            st.session_state.last_token_refresh = current_time

        except Exception as e:
            st.error(f"Database connection failed: {e}")
            st.error(f"Host: {host if 'host' in locals() else 'unknown'}")
            st.error(f"User: {db_user if 'db_user' in locals() else 'unknown'}")
            return None

    return st.session_state.conn

def get_current_user():
    """Resolve the acting user: Databricks identity in Lakebase mode, DEV_USER locally"""
    if not _is_lakebase():
        return os.environ.get('DEV_USER', 'dev@example.com')
    if w is None:
        return 'unknown'
    try:
        return w.current_user.me().user_name
    except Exception:
        return 'unknown'

def get_tickets_by_status(status, priority=None, search=None):
    """Fetch tickets by status, optionally filtered by priority and title search"""
    conn = get_db_connection()
    if not conn:
        return []
    
    conditions = ["status = %s"]
    params = [status]
    if priority:
        conditions.append("priority = %s")
        params.append(priority)
    if search:
        conditions.append("title ILIKE %s")
        params.append(f"%{search}%")
    
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT ticket_id, title, status, priority, created_by, created_at,
                   (SELECT COUNT(*) FROM ticket_messages WHERE ticket_id = tickets.ticket_id) as message_count
            FROM tickets
            WHERE {" AND ".join(conditions)}
            ORDER BY created_at DESC
        """, params)
        
        tickets = []
        for row in cur.fetchall():
            tickets.append({
                'ticket_id': row[0],
                'title': row[1],
                'status': row[2],
                'priority': row[3],
                'created_by': row[4],
                'created_at': row[5],
                'message_count': row[6]
            })
    conn.commit()
    
    return tickets

def get_ticket_messages(ticket_id):
    """Fetch messages for a ticket in chronological order"""
    conn = get_db_connection()
    if not conn:
        return []
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT message_id, message_text, author, created_at
            FROM ticket_messages
            WHERE ticket_id = %s
            ORDER BY created_at ASC
        """, (ticket_id,))
        
        messages = []
        for row in cur.fetchall():
            messages.append({
                'message_id': row[0],
                'message_text': row[1],
                'author': row[2],
                'created_at': row[3]
            })
    conn.commit()
    
    return messages

def create_ticket(title, status='open', priority='medium'):
    """Create a new ticket"""
    title = (title or '').strip()
    if not title:
        st.error("Ticket title cannot be empty")
        return None
    if status not in VALID_STATUSES or priority not in VALID_PRIORITIES:
        st.error(f"Invalid status '{status}' or priority '{priority}'")
        return None

    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        # Get current user
        current_user = get_current_user()
        
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tickets (title, status, priority, created_by)
                VALUES (%s, %s, %s, %s)
                RETURNING ticket_id
            """, (title, status, priority, current_user))
            
            ticket_id = cur.fetchone()[0]
            conn.commit()
            return ticket_id
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to create ticket: {e}")
        return None

def add_message(ticket_id, message_text):
    """Add a message to a ticket"""
    message_text = (message_text or '').strip()
    if not message_text:
        st.error("Message cannot be empty")
        return False

    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        # Get current user
        current_user = get_current_user()
        
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ticket_messages (ticket_id, message_text, author)
                VALUES (%s, %s, %s)
            """, (ticket_id, message_text, current_user))
            
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to add message: {e}")
        return False

def update_ticket_status(ticket_id, new_status):
    """Update ticket status"""
    if new_status not in VALID_STATUSES:
        st.error(f"Invalid status '{new_status}'")
        return False

    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tickets
                SET status = %s
                WHERE ticket_id = %s
            """, (new_status, ticket_id))
            
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to update ticket: {e}")
        return False

def update_ticket_priority(ticket_id, new_priority):
    """Update ticket priority"""
    if new_priority not in VALID_PRIORITIES:
        st.error(f"Invalid priority '{new_priority}'")
        return False

    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tickets
                SET priority = %s
                WHERE ticket_id = %s
            """, (new_priority, ticket_id))
            
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to update priority: {e}")
        return False

def update_ticket(ticket_id, new_status=None, new_priority=None):
    """Update a ticket's status and/or priority in a single transaction"""
    if new_status is not None and new_status not in VALID_STATUSES:
        st.error(f"Invalid status '{new_status}'")
        return False
    if new_priority is not None and new_priority not in VALID_PRIORITIES:
        st.error(f"Invalid priority '{new_priority}'")
        return False

    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tickets
                SET status = COALESCE(%s, status),
                    priority = COALESCE(%s, priority)
                WHERE ticket_id = %s
            """, (new_status, new_priority, ticket_id))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to update ticket: {e}")
        return False

VALID_STATUSES = ['open', 'in_progress', 'resolved']
VALID_PRIORITIES = ['low', 'medium', 'high', 'urgent']

def delete_ticket(ticket_id):
    """Delete a ticket (messages cascade via FK)"""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tickets WHERE ticket_id = %s", (ticket_id,))
            conn.commit()
            return True
    except Exception as e:
        conn.rollback()
        st.error(f"Failed to delete ticket: {e}")
        return False


# Atlassian-inspired priority palette (Jira: red arrows up for high/urgent,
# orange for medium, blue down arrow for low)
PRIORITY_COLORS = {
    'urgent': (222, 53, 11),    # R300
    'high': (255, 86, 48),      # R400-ish
    'medium': (255, 171, 0),    # Y300
    'low': (38, 132, 255),      # B400
}

PRIORITY_ICONS = {
    'urgent': '⇈',
    'high': '▲',
    'medium': '●',
    'low': '▼',
}

STATUS_META = {
    'open': {'label': 'Open', 'color': '#42526E', 'tint': '#DFE1E6'},
    'in_progress': {'label': 'In Progress', 'color': '#0052CC', 'tint': '#DEEBFF'},
    'resolved': {'label': 'Resolved', 'color': '#006644', 'tint': '#E3FCEF'},
}

AVATAR_COLORS = ['#0052CC', '#6554C0', '#006644', '#FF5630', '#36B37E', '#FF991F', '#00B8D9', '#FFAB00']

def avatar_color(name):
    """Deterministic avatar background color from a name/email (stable across runs)"""
    return AVATAR_COLORS[sum(ord(c) for c in (name or '?')) % len(AVATAR_COLORS)]

def time_ago(dt):
    """Human-friendly relative time for a datetime"""
    if dt is None:
        return ''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return 'just now'
    minutes = int(seconds // 60)
    if minutes < 60:
        return f'{minutes}m ago'
    hours = int(minutes // 60)
    if hours < 24:
        return f'{hours}h ago'
    days = int(hours // 24)
    if days < 30:
        return f'{days}d ago'
    months = int(days // 30)
    if months < 12:
        return f'{months}mo ago'
    return f'{months // 12}y ago'

def get_stats():
    """Aggregate ticket counts for the metrics row (unfiltered)"""
    conn = get_db_connection()
    if not conn:
        return {'total': 0, 'open': 0, 'in_progress': 0, 'resolved': 0, 'urgent': 0}
    with conn.cursor() as cur:
        cur.execute("SELECT status, priority, COUNT(*) FROM tickets GROUP BY status, priority")
        rows = cur.fetchall()
    conn.commit()
    stats = {'total': 0, 'open': 0, 'in_progress': 0, 'resolved': 0, 'urgent': 0}
    for status, priority, count in rows:
        stats['total'] += count
        if status in stats:
            stats[status] += count
        if priority == 'urgent':
            stats['urgent'] += count
    return stats

def render_metric(label, value, color):
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-accent" style="background:{color}"></div>
            <div class="metric-body">
                <div class="metric-value">{value}</div>
                <div class="metric-label">{label}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

APP_CSS = """
<style>
/* ---------- Atlassian design tokens ---------- */
:root {
    --n0: #ffffff;
    --n10: #fafbfc;
    --n20: #f4f5f7;
    --n30: #ebecf0;
    --n40: #dfe1e6;
    --n400: #6b778c;
    --n500: #42526e;
    --n700: #253858;
    --n800: #172b4d;
    --b400: #0052cc;
    --b500: #0747a6;
    --b75: #deebff;
    --b50: #eaf2ff;
    --r300: #de350b;
    --r75: #ffebe6;
    --g300: #36b37e;
    --g75: #e3fcef;
    --y300: #ffab00;
    --card-shadow: 0 1px 1px rgba(9, 30, 66, 0.25), 0 0 1px rgba(9, 30, 66, 0.31);
    --card-shadow-hover: 0 4px 8px -2px rgba(9, 30, 66, 0.25), 0 0 1px rgba(9, 30, 66, 0.31);
}

.stApp { background: var(--n0); }
[data-testid="stHeader"] { background: transparent; }
#MainMenu { display: none; }
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Noto Sans',
                 'Ubuntu', 'Droid Sans', 'Helvetica Neue', sans-serif;
    color: var(--n800);
}

/* ---------- Sidebar (Jira left nav) ---------- */
section[data-testid="stSidebar"] {
    background: var(--n10);
    border-right: 1px solid var(--n40);
}
section[data-testid="stSidebar"] .stButton > button { width: 100%; }

.sb-project { display: flex; align-items: center; gap: 10px; padding: 6px 2px 14px 2px; }
.sb-project-avatar {
    width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    background: var(--b400); color: #fff;
    font-size: 16px; font-weight: 700;
    border-radius: 3px;
}
.sb-project-name { font-size: 14px; font-weight: 600; color: var(--n800); line-height: 1.2; }
.sb-project-sub { font-size: 12px; color: var(--n400); }
.sb-section {
    font-size: 11px; font-weight: 600; letter-spacing: 0.05em;
    text-transform: uppercase; color: var(--n400);
    margin: 16px 2px 6px 2px;
}

/* ---------- Topbar (Jira nav) ---------- */
.topbar {
    display: flex; align-items: center; gap: 12px;
    margin: 0 0 18px 0;
    padding: 10px 16px;
    background: var(--b500);
    color: #fff;
    border-radius: 3px;
    box-shadow: var(--card-shadow);
}
.topbar-logo {
    width: 28px; height: 28px;
    display: flex; align-items: center; justify-content: center;
    background: rgba(255, 255, 255, 0.18);
    border-radius: 3px;
    font-size: 15px;
}
.topbar-title { font-size: 16px; font-weight: 600; letter-spacing: 0.01em; }
.topbar-tag { font-size: 13px; color: rgba(255, 255, 255, 0.75); margin-left: 6px; }

/* ---------- Metrics ---------- */
.metric-card {
    display: flex; align-items: stretch; gap: 12px;
    background: var(--n0);
    border-radius: 3px;
    box-shadow: var(--card-shadow);
    overflow: hidden;
    padding: 0;
    margin-bottom: 2px;
}
.metric-accent { width: 4px; flex-shrink: 0; }
.metric-body { padding: 12px 14px; }
.metric-value { font-size: 22px; font-weight: 600; color: var(--n800); line-height: 1.1; }
.metric-label {
    font-size: 11px; font-weight: 600;
    color: var(--n400);
    text-transform: uppercase; letter-spacing: 0.05em;
    margin-top: 2px;
}

/* ---------- Board columns (Jira) ---------- */
.board-column-header {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 8px 10px 8px;
}
.col-title {
    font-size: 12px; font-weight: 600;
    letter-spacing: 0.05em; text-transform: uppercase;
    color: var(--n400);
}
.col-count {
    font-size: 12px; font-weight: 600;
    color: var(--n400);
    background: var(--n30);
    border-radius: 3px;
    padding: 1px 7px;
    margin-left: auto;
}
.st-key-col_open,
.st-key-col_in_progress,
.st-key-col_resolved {
    background: var(--n20);
    border: none !important;
    border-radius: 3px !important;
    box-shadow: none !important;
}
.st-key-col_open [data-testid="stVerticalBlockBorderWrapper"],
.st-key-col_in_progress [data-testid="stVerticalBlockBorderWrapper"],
.st-key-col_resolved [data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--n20);
    border: none;
    border-radius: 3px;
}

/* ---------- Ticket cards (Jira issue cards) ---------- */
.ticket-card {
    background: var(--n0);
    border-radius: 3px;
    box-shadow: var(--card-shadow);
    padding: 10px 12px 8px 12px;
    margin: 6px 0;
    cursor: pointer;
    transition: box-shadow 0.1s ease;
}
.ticket-card:hover { box-shadow: var(--card-shadow-hover); background: var(--n10); }
.ticket-title {
    font-size: 14px; font-weight: 500; color: var(--n800);
    line-height: 1.4;
    margin-bottom: 10px;
}
.ticket-card-bottom {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.ticket-card-meta {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; color: var(--n400);
}
.loz {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 11px; font-weight: 600;
    letter-spacing: 0.03em; text-transform: uppercase;
    padding: 2px 6px;
    border-radius: 3px;
    white-space: nowrap;
}
.avatar {
    width: 24px; height: 24px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    border-radius: 50%;
    color: #fff;
    font-size: 11px; font-weight: 700;
    text-transform: uppercase;
}
.ticket-card .stButton > button {
    background: transparent;
    border: none;
    color: var(--n400);
    font-size: 12px; font-weight: 500;
    border-radius: 3px;
    padding: 2px 8px;
    width: auto;
}
.ticket-card .stButton > button:hover {
    background: var(--b50);
    color: var(--b400);
}

/* Clickable whole card: an invisible full-size button overlays the card,
   while the DnD handle (z-index 10) stays above it so dragging still works. */
[class*="st-key-ticket_"] { position: relative; }
[class*="st-key-ticket_"] > [data-testid="stElementContainer"] { position: static; }
[class*="st-key-ticket_"] [data-testid="stButton"] {
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    z-index: 5;
}
[class*="st-key-ticket_"] [data-testid="stButton"] > button {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    width: 100%;
    height: 100%;
    padding: 0;
    margin: 0;
    color: transparent;
    cursor: pointer;
}
[class*="st-key-ticket_"]:hover .ticket-card {
    box-shadow: var(--card-shadow-hover);
    background: var(--n10);
}
[class*="st-key-ticket_"] .stdnd-handle { z-index: 20; }

/* ---------- Widgets ---------- */
.stButton > button {
    border-radius: 3px;
    font-weight: 500;
    border: none;
    background: var(--n20);
    color: var(--n800);
}
.stButton > button:hover { background: var(--n30); }
.stButton > button[kind="primary"] {
    background: var(--b400) !important;
    color: #fff !important;
}
.stButton > button[kind="primary"]:hover { background: var(--b500) !important; }
.stTextInput > div > div > input, .stTextArea textarea {
    border-radius: 3px;
    border: 2px solid var(--n40);
}
.stTextInput > div > div > input:focus, .stTextArea textarea:focus {
    border-color: var(--b400);
    box-shadow: none;
}
.stSelectbox > div > div { border-radius: 3px; border: 2px solid var(--n40); }
.stExpander {
    border: none !important;
    border-radius: 3px !important;
    background: var(--n20);
}

/* ---------- Dialog (Jira issue view) ---------- */
[data-testid="stDialog"] {
    border-radius: 3px;
    box-shadow: 0 8px 16px -4px rgba(9, 30, 66, 0.31), 0 0 1px rgba(9, 30, 66, 0.31);
    border: none;
}
.dialog-head {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--n40);
    margin-bottom: 14px;
}
.dialog-title { font-size: 18px; font-weight: 600; color: var(--n800); }
.dialog-badges { display: flex; gap: 8px; margin-left: auto; }
.dialog-meta { width: 100%; font-size: 13px; color: var(--n400); }

/* ---------- Comments (Jira style) ---------- */
.comment { display: flex; gap: 10px; margin: 14px 0; }
.comment-avatar {
    width: 32px; height: 32px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    border-radius: 50%;
    color: #fff;
    font-size: 13px; font-weight: 700;
    text-transform: uppercase;
}
.comment-body { flex: 1; min-width: 0; }
.comment-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 2px; }
.comment-author { font-size: 14px; font-weight: 600; color: var(--n800); }
.comment-you { font-size: 11px; color: var(--b400); font-weight: 500; }
.comment-time { font-size: 12px; color: var(--n400); }
.comment-body [data-testid="stMarkdownContainer"] {
    font-size: 14px;
    color: var(--n800);
    line-height: 1.5;
    word-wrap: break-word;
}
.comment-body [data-testid="stMarkdown"] p { margin: 0 0 4px 0; }
.comment-body [data-testid="stMarkdown"] p:last-child { margin-bottom: 0; }
.msg-empty { color: var(--n400); font-size: 14px; }

/* ---------- Danger zone ---------- */
.danger-btn .stButton > button {
    color: var(--r300) !important;
    background: var(--r75) !important;
}
.danger-btn .stButton > button:hover { background: #ffbdad !important; }
</style>
"""

def render_ticket_card(ticket):
    """Render a ticket card"""
    priority = ticket['priority']
    r, g, b = PRIORITY_COLORS.get(priority, PRIORITY_COLORS['medium'])
    author = ticket['created_by'] or 'unknown'
    author_short = author.split('@')[0]
    st.markdown(
        f"""
        <div class="ticket-card">
            <div class="ticket-title">{html.escape(ticket['title'])}</div>
            <div class="ticket-card-bottom">
                <div class="ticket-card-meta">
                    <span>#{ticket['ticket_id']}</span>
                    <span class="loz" style="color:rgb({r},{g},{b});background:rgba({r},{g},{b},0.14)">
                        {PRIORITY_ICONS.get(priority, '')} {html.escape(priority)}
                    </span>
                    <span title="{ticket['message_count']} messages">💬 {ticket['message_count']}</span>
                </div>
                <div class="avatar" style="background:{avatar_color(author)}" title="{html.escape(author)}">
                    {html.escape(author_short[0].upper())}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("", key=f"open_{ticket['ticket_id']}", use_container_width=True):
        st.session_state.show_new_ticket = False
        st.session_state.selected_ticket = ticket['ticket_id']

def render_message(msg, current_user):
    author = (msg['author'] or 'anonymous')
    mine = author == current_user
    you_badge = '<span class="comment-you">· You</span>' if mine else ''
    st.markdown(
        f"""<div class="comment">
            <div class="comment-avatar" style="background:{avatar_color(author)}">{html.escape(author[0].upper())}</div>
            <div class="comment-body">
                <div class="comment-head">
                    <span class="comment-author">{html.escape(author)}</span>{you_badge}
                    <span class="comment-time">{time_ago(msg['created_at'])}</span>
                </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(msg['message_text'] or '')
    st.markdown('</div></div>', unsafe_allow_html=True)

def show_ticket_modal():
    """Show ticket details modal"""
    if not st.session_state.get('selected_ticket'):
        return
    
    ticket_id = st.session_state.selected_ticket
    
    # Get ticket details
    conn = get_db_connection()
    if not conn:
        return
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ticket_id, title, status, priority, created_by, created_at
            FROM tickets
            WHERE ticket_id = %s
        """, (ticket_id,))
        
        row = cur.fetchone()
        if not row:
            st.error("Ticket not found")
            return
        
        ticket = {
            'ticket_id': row[0],
            'title': row[1],
            'status': row[2],
            'priority': row[3],
            'created_by': row[4],
            'created_at': row[5]
        }
    conn.commit()
    
    current_user = get_current_user() or 'unknown'

    # Show modal
    @st.dialog(f"Ticket #{ticket['ticket_id']}", width="large")
    def ticket_dialog():
        status_meta = STATUS_META.get(ticket['status'], STATUS_META['open'])
        pr, pg, pb = PRIORITY_COLORS.get(ticket['priority'], PRIORITY_COLORS['medium'])
        st.markdown(
            f"""
            <div class="dialog-head">
                <div class="dialog-title">{html.escape(ticket['title'])}</div>
                <div class="dialog-badges">
                    <span class="loz" style="color:{status_meta['color']};background:{status_meta['tint']}">{html.escape(ticket['status'].replace('_', ' '))}</span>
                    <span class="loz" style="color:rgb({pr},{pg},{pb});background:rgba({pr},{pg},{pb},0.14)">{PRIORITY_ICONS.get(ticket['priority'], '')} {html.escape(ticket['priority'])}</span>
                </div>
                <div class="dialog-meta">Ticket #{ticket['ticket_id']} · created by {html.escape(ticket['created_by'] or 'unknown')} · {ticket['created_at'].strftime('%b %d, %Y %H:%M') if ticket['created_at'] else ''}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        def save_on_change():
            new_status = st.session_state.get(f"status_{ticket_id}")
            new_priority = st.session_state.get(f"priority_{ticket_id}")
            if update_ticket(ticket_id, new_status, new_priority):
                st.session_state.ticket_updated = True

        c1, c2 = st.columns(2)
        with c1:
            st.selectbox(
                "Status",
                options=VALID_STATUSES,
                index=VALID_STATUSES.index(ticket['status']),
                key=f"status_{ticket_id}",
                on_change=save_on_change,
            )
        with c2:
            st.selectbox(
                "Priority",
                options=VALID_PRIORITIES,
                index=VALID_PRIORITIES.index(ticket['priority']),
                key=f"priority_{ticket_id}",
                on_change=save_on_change,
            )

        if st.session_state.pop("ticket_updated", False):
            st.rerun()

        st.divider()

        # Messages
        st.markdown('<div class="sb-section" style="margin:0 0 8px 0">Comments</div>', unsafe_allow_html=True)
        messages = get_ticket_messages(ticket_id)

        if messages:
            for msg in messages:
                render_message(msg, current_user)
        else:
            st.markdown('<div class="msg-empty">No comments yet — start the conversation.</div>', unsafe_allow_html=True)

        st.divider()

        # Add new message
        st.markdown('<div class="sb-section" style="margin:0 0 8px 0">Add Comment</div>', unsafe_allow_html=True)
        new_message = st.text_area("Message", key=f"new_message_{ticket_id}", placeholder="Write a reply…")
        if st.button("Send Message", key=f"send_{ticket_id}", type="primary", disabled=not new_message.strip()):
            if add_message(ticket_id, new_message):
                st.rerun()

        st.divider()

        # Danger zone
        st.markdown(
            '<div class="danger-btn">',
            unsafe_allow_html=True,
        )
        if st.button("Delete Ticket", key=f"delete_{ticket_id}"):
            st.session_state[f"confirming_delete_{ticket_id}"] = True
        st.markdown('</div>', unsafe_allow_html=True)
        if st.session_state.get(f"confirming_delete_{ticket_id}"):
            st.warning("Are you sure? This permanently deletes the ticket and all its messages.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Yes, Delete", key=f"confirm_delete_{ticket_id}", type="primary"):
                    if delete_ticket(ticket_id):
                        st.session_state[f"confirming_delete_{ticket_id}"] = False
                        st.session_state.selected_ticket = None
                        st.rerun()
            with c2:
                if st.button("Cancel", key=f"cancel_delete_{ticket_id}"):
                    st.session_state[f"confirming_delete_{ticket_id}"] = False
                    st.rerun()
    
    ticket_dialog()

# Main app
st.markdown(APP_CSS, unsafe_allow_html=True)

# Sidebar
with st.sidebar:
    st.markdown(
        """
        <div class="sb-project">
            <div class="sb-project-avatar">🎫</div>
            <div>
                <div class="sb-project-name">Ticketing System</div>
                <div class="sb-project-sub">Support queue</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.markdown('<div class="sb-section">Board</div>', unsafe_allow_html=True)
    if st.button("+ New Ticket", key="new_ticket_btn", type="primary", use_container_width=True):
        st.session_state.selected_ticket = None
        st.session_state.show_new_ticket = True

    st.markdown('<div class="sb-section">Filters</div>', unsafe_allow_html=True)
    filter_priority = st.selectbox(
        "Priority",
        options=["All"] + VALID_PRIORITIES,
        index=0,
        key="filter_priority",
        label_visibility="collapsed",
        placeholder="All priorities",
    )
    filter_search = st.text_input("Search", key="filter_search", placeholder="Search by title…",
                                  label_visibility="collapsed")

priority_filter = None if filter_priority == "All" else filter_priority
search_filter = filter_search.strip() or None

# New ticket dialog (mutually exclusive with the ticket modal)
if st.session_state.get('show_new_ticket') and not st.session_state.get('selected_ticket'):
    @st.dialog("Create New Ticket", width="small")
    def new_ticket_dialog():
        new_ticket_title = st.text_input("Ticket Title", key="new_ticket_title")
        new_ticket_status = st.selectbox(
            "Initial Status",
            options=VALID_STATUSES,
            index=0,
            key="new_ticket_status"
        )
        new_ticket_priority = st.selectbox(
            "Priority",
            options=VALID_PRIORITIES,
            index=1,
            key="new_ticket_priority"
        )

        if st.button("Create Ticket", key="create_ticket", type="primary",
                     use_container_width=True, disabled=not new_ticket_title.strip()):
            ticket_id = create_ticket(new_ticket_title, new_ticket_status, new_ticket_priority)
            if ticket_id:
                st.session_state.show_new_ticket = False
                st.rerun()
    new_ticket_dialog()

# Topbar (Jira-style nav)
st.markdown(
    """
    <div class="topbar">
        <div class="topbar-logo">🎫</div>
        <div class="topbar-title">Ticketing System</div>
        <div class="topbar-tag">Support board · drag tickets between columns to change status</div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Metrics row
stats = get_stats()
metric_cols = st.columns(4)
with metric_cols[0]:
    render_metric("Open", stats['open'], STATUS_META['open']['color'])
with metric_cols[1]:
    render_metric("In Progress", stats['in_progress'], STATUS_META['in_progress']['color'])
with metric_cols[2]:
    render_metric("Resolved", stats['resolved'], STATUS_META['resolved']['color'])
with metric_cols[3]:
    render_metric("Urgent", stats['urgent'], '#de350b')

st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

# Kanban board (drag & drop)
open_tickets = get_tickets_by_status('open', priority=priority_filter, search=search_filter)
in_progress_tickets = get_tickets_by_status('in_progress', priority=priority_filter, search=search_filter)
resolved_tickets = get_tickets_by_status('resolved', priority=priority_filter, search=search_filter)

COLUMN_STATUS = {
    "col_open": "open",
    "col_in_progress": "in_progress",
    "col_resolved": "resolved",
}

def render_column(key, label, tickets):
    st.markdown(
        f"""
        <div class="board-column-header">
            <span class="col-title">{label}</span>
            <span class="col-count">{len(tickets)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.container(key=key, border=True):
        for ticket in tickets:
            with st.container(key=f"ticket_{ticket['ticket_id']}", border=True):
                render_ticket_card(ticket)

col1, col2, col3 = st.columns(3)
with col1:
    render_column("col_open", STATUS_META['open']['label'], open_tickets)
with col2:
    render_column("col_in_progress", STATUS_META['in_progress']['label'], in_progress_tickets)
with col3:
    render_column("col_resolved", STATUS_META['resolved']['label'], resolved_tickets)

event = dnd("col_open", "col_in_progress", "col_resolved",
            handle=True, handle_icon=":material/drag_indicator:", key="kanban_dnd")
if event and event.item_key and event.item_key.startswith("ticket_"):
    drop_sig = (event.from_container, event.to_container, event.item_key, event.from_index, event.to_index)
    if st.session_state.get("last_drop") != drop_sig:
        st.session_state.last_drop = drop_sig
        st.session_state.selected_ticket = None
        ticket_id = int(event.item_key.split("_", 1)[1])
        target_status = COLUMN_STATUS.get(event.to_container)
        if target_status and update_ticket_status(ticket_id, target_status):
            st.rerun()

# Show modal if a ticket is selected
show_ticket_modal()