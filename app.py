import streamlit as st
import psycopg
import os
import time
import html
from databricks.sdk import WorkspaceClient
from datetime import datetime, timezone
import pandas as pd
from streamlit_dnd import dnd

# Page configuration
st.set_page_config(page_title="Ticketing System", page_icon="🎫", layout="wide", initial_sidebar_state="expanded")

# Initialize Databricks client and connection
w = WorkspaceClient()

# Global connection state
if 'conn' not in st.session_state or 'last_token_refresh' not in st.session_state:
    st.session_state.last_token_refresh = 0
    st.session_state.conn = None

def get_oauth_token():
    """Get a fresh Lakebase OAuth token for the bound database endpoint"""
    endpoint = os.environ.get('LAKEBASE_ENDPOINT') or os.environ.get('ENDPOINT_NAME')
    if not endpoint:
        raise ValueError("LAKEBASE_ENDPOINT not found in environment")

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
            host = os.environ.get('PGHOST')
            port = os.environ.get('PGPORT', '5432')
            dbname = os.environ.get('PGDATABASE', 'databricks_postgres')
            db_user = os.environ.get('PGUSER')
            sslmode = os.environ.get('PGSSLMODE', 'require')

            if not all([host, db_user]):
                raise ValueError("PGHOST/PGUSER not found in environment")

            # Get fresh OAuth token
            token = get_oauth_token()

            # Create new connection
            st.session_state.conn = psycopg.connect(
                host=host,
                dbname=dbname,
                user=db_user,
                port=port,
                password=token,
                sslmode=sslmode,
                connect_timeout=10
            )
            ensure_schema(st.session_state.conn)
            st.session_state.last_token_refresh = current_time

        except Exception as e:
            st.error(f"Database connection failed: {e}")
            st.error(f"Host: {host if 'host' in locals() else 'unknown'}")
            st.error(f"User: {db_user if 'db_user' in locals() else 'unknown'}")
            return None

    return st.session_state.conn

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
        current_user = w.current_user.me().user_name
        
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
        current_user = w.current_user.me().user_name
        
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


PRIORITY_COLORS = {
    'urgent': (244, 67, 54),
    'high': (255, 152, 0),
    'medium': (255, 193, 7),
    'low': (76, 175, 80),
}

STATUS_META = {
    'open': {'label': 'Open', 'color': '#6b5bff', 'icon': '📋'},
    'in_progress': {'label': 'In Progress', 'color': '#f59e0b', 'icon': '🔄'},
    'resolved': {'label': 'Resolved', 'color': '#22c55e', 'icon': '✅'},
}

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
    stats = {'total': 0, 'open': 0, 'in_progress': 0, 'resolved': 0, 'urgent': 0}
    for status, priority, count in rows:
        stats['total'] += count
        if status in stats:
            stats[status] += count
        if priority == 'urgent':
            stats['urgent'] += count
    return stats

def render_metric(label, value, color, icon):
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-icon" style="background:{color}1a;color:{color}">{icon}</div>
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
:root {
    --accent: #6b5bff;
    --accent-dark: #5748e0;
    --accent-soft: #eeecff;
    --bg: #f4f5fb;
    --card: #ffffff;
    --border: #e4e6f1;
    --text: #1c2033;
    --muted: #6a708c;
    --shadow: 0 1px 2px rgba(28, 32, 51, 0.05), 0 4px 14px rgba(28, 32, 51, 0.06);
}

/* ---------- App shell ---------- */
.stApp { background: var(--bg); }
[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"] { display: none; }
#MainMenu { display: none; }

section[data-testid="stSidebar"] {
    background: #ffffff;
    border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] .stButton > button {
    width: 100%;
    border-radius: 10px;
    font-weight: 600;
}

/* ---------- Typography ---------- */
h1, h2, h3, h4 { color: var(--text); letter-spacing: -0.01em; }

/* ---------- Header ---------- */
.app-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 22px 6px 18px 6px;
    margin-bottom: 8px;
}
.app-header-left { display: flex; align-items: center; gap: 14px; }
.app-logo {
    width: 46px; height: 46px;
    display: flex; align-items: center; justify-content: center;
    font-size: 24px;
    background: linear-gradient(135deg, #6b5bff, #8b5cf6);
    color: #fff;
    border-radius: 12px;
    box-shadow: var(--shadow);
}
.app-title { font-size: 24px; font-weight: 700; color: var(--text); line-height: 1.1; }
.app-subtitle { font-size: 13px; color: var(--muted); margin-top: 2px; }
.app-header-right { display: flex; align-items: center; gap: 10px; }
.app-header-right .stButton > button {
    border-radius: 10px;
    font-weight: 600;
    padding: 0.45rem 1.1rem;
}

/* ---------- Metrics ---------- */
.metric-card {
    flex: 1;
    display: flex; align-items: center; gap: 12px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 14px 16px;
    box-shadow: var(--shadow);
}
.metric-icon {
    width: 40px; height: 40px;
    display: flex; align-items: center; justify-content: center;
    font-size: 19px;
    border-radius: 10px;
}
.metric-value { font-size: 22px; font-weight: 700; color: var(--text); line-height: 1.1; }
.metric-label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }

/* ---------- Board columns ---------- */
.board-column-header {
    display: flex; align-items: center; gap: 8px;
    padding: 4px 6px 12px 6px;
}
.status-dot { width: 9px; height: 9px; border-radius: 50%; }
.col-title { font-size: 15px; font-weight: 700; color: var(--text); }
.col-count {
    margin-left: auto;
    background: #eef0f7;
    color: var(--muted);
    font-size: 12px; font-weight: 600;
    padding: 2px 9px;
    border-radius: 999px;
}
.st-key-col_open,
.st-key-col_in_progress,
.st-key-col_resolved {
    background: transparent;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    box-shadow: var(--shadow);
}

/* ---------- Ticket cards ---------- */
.ticket-card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    margin: 6px 0;
    transition: box-shadow 0.15s ease, transform 0.15s ease;
}
.ticket-card:hover {
    box-shadow: 0 6px 18px rgba(28, 32, 51, 0.10);
    transform: translateY(-1px);
}
.ticket-card-top {
    display: flex; align-items: flex-start; justify-content: space-between; gap: 8px;
    margin-bottom: 8px;
}
.ticket-title { font-size: 14px; font-weight: 600; color: var(--text); line-height: 1.35; }
.priority-badge {
    flex-shrink: 0;
    font-size: 10px; font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 999px;
    white-space: nowrap;
}
.ticket-card-meta {
    display: flex; flex-wrap: wrap; gap: 10px;
    font-size: 12px; color: var(--muted);
}
.ticket-card .stButton > button {
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    background: #f4f5fb;
    border: 1px solid var(--border);
    color: var(--text);
}
.ticket-card .stButton > button:hover {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent-dark);
}

/* ---------- Widgets ---------- */
.stButton > button { border-radius: 10px; font-weight: 600; }
.stTextInput > div > div > input, .stTextArea textarea {
    border-radius: 10px;
    border: 1px solid var(--border);
}
.stSelectbox > div > div { border-radius: 10px; border: 1px solid var(--border); }
.stExpander {
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    background: var(--card);
}

/* ---------- Dialog ---------- */
[data-testid="stDialog"] {
    border-radius: 18px;
    box-shadow: 0 20px 60px rgba(28, 32, 51, 0.25);
    border: 1px solid var(--border);
}
[data-testid="stDialog"] [data-testid="stVerticalBlock"] { padding: 6px; }

.dialog-head {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 2px 2px 14px 2px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 14px;
}
.dialog-title { font-size: 19px; font-weight: 700; color: var(--text); }
.dialog-badges { display: flex; gap: 8px; margin-left: auto; }
.status-badge {
    font-size: 11px; font-weight: 700;
    letter-spacing: 0.04em; text-transform: capitalize;
    padding: 4px 11px;
    border-radius: 999px;
}
.dialog-meta { width: 100%; font-size: 13px; color: var(--muted); }

/* ---------- Message bubbles ---------- */
.msg-row { display: flex; gap: 10px; margin: 10px 0; }
.msg-row.msg-mine { flex-direction: row-reverse; }
.msg-avatar {
    width: 34px; height: 34px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    border-radius: 50%;
    background: var(--accent-soft);
    color: var(--accent-dark);
    font-size: 14px; font-weight: 700;
}
.msg-bubble {
    max-width: 75%;
    background: #f2f3f9;
    border: 1px solid var(--border);
    border-radius: 14px;
    border-top-left-radius: 4px;
    padding: 9px 13px;
}
.msg-row.msg-mine .msg-bubble {
    background: var(--accent-soft);
    border-color: #dcd8ff;
    border-radius: 14px;
    border-top-right-radius: 4px;
}
.msg-head { display: flex; align-items: center; gap: 8px; margin-bottom: 3px; }
.msg-author { font-size: 12px; font-weight: 700; color: var(--text); }
.msg-time { font-size: 11px; color: var(--muted); }
.msg-text { font-size: 14px; color: var(--text); line-height: 1.45; word-wrap: break-word; }
.msg-empty { color: var(--muted); font-size: 14px; }

/* ---------- Danger zone ---------- */
.danger-btn .stButton > button {
    color: #dc2626 !important;
    border-color: #fecaca !important;
    background: #fef2f2 !important;
}
.danger-btn .stButton > button:hover { background: #fee2e2 !important; }
</style>
"""

def render_ticket_card(ticket):
    """Render a ticket card"""
    priority = ticket['priority']
    r, g, b = PRIORITY_COLORS.get(priority, PRIORITY_COLORS['medium'])
    author = (ticket['created_by'] or 'unknown').split('@')[0]
    st.markdown(
        f"""
        <div class="ticket-card">
            <div class="ticket-card-top">
                <div class="ticket-title">{html.escape(ticket['title'])}</div>
                <span class="priority-badge" style="color:rgb({r},{g},{b});background:rgba({r},{g},{b},0.12)">{html.escape(priority)}</span>
            </div>
            <div class="ticket-card-meta">
                <span>#{ticket['ticket_id']}</span>
                <span>💬 {ticket['message_count']}</span>
                <span>👤 {html.escape(author)}</span>
                <span>🕒 {time_ago(ticket['created_at'])}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if st.button("View Details", key=f"view_{ticket['ticket_id']}", use_container_width=True):
        st.session_state.selected_ticket = ticket['ticket_id']

def render_message(msg, current_user):
    author = (msg['author'] or 'anonymous')
    mine = author == current_user
    avatar = html.escape(author[0].upper())
    cls = 'msg-mine' if mine else ''
    st.markdown(
        f"""
        <div class="msg-row {cls}">
            <div class="msg-avatar">{avatar}</div>
            <div class="msg-bubble">
                <div class="msg-head">
                    <span class="msg-author">{html.escape(author)}</span>
                    <span class="msg-time">{time_ago(msg['created_at'])}</span>
                </div>
                <div class="msg-text">{html.escape(msg['message_text'])}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

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
    
    try:
        current_user = w.current_user.me().user_name
    except Exception:
        current_user = None

    # Show modal
    @st.dialog(f"Ticket #{ticket['ticket_id']}", width="large")
    def ticket_dialog():
        status_color = STATUS_META.get(ticket['status'], {}).get('color', '#6b5bff')
        pr, pg, pb = PRIORITY_COLORS.get(ticket['priority'], PRIORITY_COLORS['medium'])
        st.markdown(
            f"""
            <div class="dialog-head">
                <div class="dialog-title">Ticket #{ticket['ticket_id']}</div>
                <div class="dialog-badges">
                    <span class="status-badge" style="color:{status_color};background:{status_color}1a">{html.escape(ticket['status'].replace('_', ' '))}</span>
                    <span class="priority-badge" style="color:rgb({pr},{pg},{pb});background:rgba({pr},{pg},{pb},0.12)">{html.escape(ticket['priority'])}</span>
                </div>
                <div class="dialog-meta">{html.escape(ticket['title'])} · created by {html.escape(ticket['created_by'] or 'unknown')} · {ticket['created_at'].strftime('%b %d, %Y %H:%M') if ticket['created_at'] else ''}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        c1, c2 = st.columns(2)
        with c1:
            new_status = st.selectbox(
                "Status",
                options=VALID_STATUSES,
                index=VALID_STATUSES.index(ticket['status']),
                key=f"status_{ticket_id}"
            )
            if st.button("Update Status", key=f"update_status_{ticket_id}", disabled=new_status == ticket['status']):
                if update_ticket_status(ticket_id, new_status):
                    st.session_state.selected_ticket = None
                    st.rerun()
        with c2:
            new_priority = st.selectbox(
                "Priority",
                options=VALID_PRIORITIES,
                index=VALID_PRIORITIES.index(ticket['priority']),
                key=f"priority_{ticket_id}"
            )
            if st.button("Update Priority", key=f"update_priority_{ticket_id}", disabled=new_priority == ticket['priority']):
                if update_ticket_priority(ticket_id, new_priority):
                    st.rerun()

        st.divider()

        # Messages
        st.markdown("##### Conversation")
        messages = get_ticket_messages(ticket_id)

        if messages:
            for msg in messages:
                render_message(msg, current_user)
        else:
            st.markdown('<div class="msg-empty">No messages yet — start the conversation.</div>', unsafe_allow_html=True)

        st.divider()

        # Add new message
        st.markdown("##### Add Message")
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
    st.markdown('<div class="app-title" style="font-size:18px">🎫 Ticketing System</div>', unsafe_allow_html=True)
    st.caption("Support queue · Databricks bootcamp")
    st.divider()

    if st.button("+ New Ticket", key="new_ticket_btn", type="primary", use_container_width=True):
        st.session_state.show_new_ticket = True

    st.markdown("#### Filters")
    filter_priority = st.selectbox(
        "Priority",
        options=["All"] + VALID_PRIORITIES,
        index=0,
        key="filter_priority",
    )
    filter_search = st.text_input("Search", key="filter_search", placeholder="Search by title…")

priority_filter = None if filter_priority == "All" else filter_priority
search_filter = filter_search.strip() or None

# New ticket dialog
if st.session_state.get('show_new_ticket'):
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

# Header
st.markdown(
    """
    <div class="app-header">
        <div class="app-header-left">
            <div class="app-logo">🎫</div>
            <div>
                <div class="app-title">Ticketing System</div>
                <div class="app-subtitle">Support queue · drag tickets between columns to change status</div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# Metrics row
stats = get_stats()
metric_cols = st.columns(4)
with metric_cols[0]:
    render_metric("Open", stats['open'], STATUS_META['open']['color'], STATUS_META['open']['icon'])
with metric_cols[1]:
    render_metric("In Progress", stats['in_progress'], STATUS_META['in_progress']['color'], STATUS_META['in_progress']['icon'])
with metric_cols[2]:
    render_metric("Resolved", stats['resolved'], STATUS_META['resolved']['color'], STATUS_META['resolved']['icon'])
with metric_cols[3]:
    render_metric("Urgent", stats['urgent'], '#f44336', '🚨')

st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

# Kanban board (drag & drop)
open_tickets = get_tickets_by_status('open', priority=priority_filter, search=search_filter)
in_progress_tickets = get_tickets_by_status('in_progress', priority=priority_filter, search=search_filter)
resolved_tickets = get_tickets_by_status('resolved', priority=priority_filter, search=search_filter)

COLUMN_STATUS = {
    "col_open": "open",
    "col_in_progress": "in_progress",
    "col_resolved": "resolved",
}

def render_column(key, label, tickets, color, icon):
    st.markdown(
        f"""
        <div class="board-column-header">
            <span class="status-dot" style="background:{color}"></span>
            <span class="col-title">{icon} {label}</span>
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
    render_column("col_open", STATUS_META['open']['label'], open_tickets,
                  STATUS_META['open']['color'], STATUS_META['open']['icon'])
with col2:
    render_column("col_in_progress", STATUS_META['in_progress']['label'], in_progress_tickets,
                  STATUS_META['in_progress']['color'], STATUS_META['in_progress']['icon'])
with col3:
    render_column("col_resolved", STATUS_META['resolved']['label'], resolved_tickets,
                  STATUS_META['resolved']['color'], STATUS_META['resolved']['icon'])

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