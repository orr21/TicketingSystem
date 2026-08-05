import streamlit as st
import psycopg
import os
import time
from databricks.sdk import WorkspaceClient
from datetime import datetime
import pandas as pd
from streamlit_dnd import dnd

# Page configuration
st.set_page_config(page_title="Ticketing System", page_icon="🎫", layout="wide")

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

def get_tickets_by_status(status):
    """Fetch tickets by status"""
    conn = get_db_connection()
    if not conn:
        return []
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ticket_id, title, status, priority, created_by, created_at,
                   (SELECT COUNT(*) FROM ticket_messages WHERE ticket_id = tickets.ticket_id) as message_count
            FROM tickets
            WHERE status = %s
            ORDER BY created_at DESC
        """, (status,))
        
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

PRIORITY_COLORS = {
    'urgent': (244, 67, 54),
    'high': (255, 152, 0),
    'medium': (255, 193, 7),
    'low': (76, 175, 80),
}

def render_ticket_card(ticket):
    """Render a ticket card"""
    r, g, b = PRIORITY_COLORS.get(ticket['priority'], PRIORITY_COLORS['medium'])
    st.markdown(f"**{ticket['title']}**")
    st.caption(f"#{ticket['ticket_id']} • {ticket['message_count']} messages")
    st.markdown(
        f"<small>Priority: <strong style='color: rgb({r}, {g}, {b})'>{ticket['priority']}</strong>"
        f" · {ticket['created_by']}</small>",
        unsafe_allow_html=True,
    )
    if st.button("View Details", key=f"view_{ticket['ticket_id']}", use_container_width=True):
        st.session_state.selected_ticket = ticket['ticket_id']

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
    
    # Show modal
    @st.dialog(f"Ticket #{ticket['ticket_id']}: {ticket['title']}", width="large")
    def ticket_dialog():
        # Ticket info
        col1, col2 = st.columns(2)
        with col1:
            st.write(f"**Created by:** {ticket['created_by']}")
            st.write(f"**Created at:** {ticket['created_at'].strftime('%Y-%m-%d %H:%M')}")
        
        with col2:
            # Status selector
            new_status = st.selectbox(
                "Status",
                options=['open', 'in_progress', 'resolved'],
                index=['open', 'in_progress', 'resolved'].index(ticket['status']),
                key=f"status_{ticket_id}"
            )
            
            if new_status != ticket['status']:
                if st.button("Update Status", key=f"update_status_{ticket_id}"):
                    if update_ticket_status(ticket_id, new_status):
                        st.success("Status updated!")
                        st.session_state.selected_ticket = None
                        st.rerun()
            
            # Priority selector
            new_priority = st.selectbox(
                "Priority",
                options=['low', 'medium', 'high', 'urgent'],
                index=['low', 'medium', 'high', 'urgent'].index(ticket['priority']),
                key=f"priority_{ticket_id}"
            )
            
            if new_priority != ticket['priority']:
                if st.button("Update Priority", key=f"update_priority_{ticket_id}"):
                    if update_ticket_priority(ticket_id, new_priority):
                        st.success("Priority updated!")
                        st.rerun()
        
        st.divider()
        
        # Messages
        st.subheader("Messages")
        messages = get_ticket_messages(ticket_id)
        
        if messages:
            for msg in messages:
                with st.container(border=True):
                    st.markdown(f"**{msg['author']}** • {msg['created_at'].strftime('%Y-%m-%d %H:%M')}")
                    st.write(msg['message_text'])
        else:
            st.info("No messages yet")
        
        st.divider()
        
        # Add new message
        st.subheader("Add Message")
        new_message = st.text_area("Message", key=f"new_message_{ticket_id}")
        
        col1, col2 = st.columns([1, 5])
        with col1:
            if st.button("Send", key=f"send_{ticket_id}", type="primary", disabled=not new_message):
                if add_message(ticket_id, new_message):
                    st.success("Message added!")
                    st.rerun()
        
        with col2:
            if st.button("Close", key=f"close_{ticket_id}"):
                st.session_state.selected_ticket = None
                st.rerun()
    
    ticket_dialog()

# Main app
st.title("🎫 Ticketing System")

# Create new ticket section
with st.expander("➕ Create New Ticket", expanded=False):
    new_ticket_title = st.text_input("Ticket Title", key="new_ticket_title")
    new_ticket_status = st.selectbox(
        "Initial Status",
        options=['open', 'in_progress', 'resolved'],
        index=0,
        key="new_ticket_status"
    )
    new_ticket_priority = st.selectbox(
        "Priority",
        options=['low', 'medium', 'high', 'urgent'],
        index=1,
        key="new_ticket_priority"
    )
    
    if st.button("Create Ticket", key="create_ticket", type="primary", disabled=not new_ticket_title):
        ticket_id = create_ticket(new_ticket_title, new_ticket_status, new_ticket_priority)
        if ticket_id:
            st.success(f"Ticket #{ticket_id} created successfully!")
            st.rerun()

st.divider()

# Kanban board (drag & drop)
st.subheader("Ticket Board")

# Fetch tickets for each status
open_tickets = get_tickets_by_status('open')
in_progress_tickets = get_tickets_by_status('in_progress')
resolved_tickets = get_tickets_by_status('resolved')

COLUMN_STATUS = {
    "col_open": "open",
    "col_in_progress": "in_progress",
    "col_resolved": "resolved",
}

def render_column(key, label, tickets):
    st.markdown(f"### {label}")
    st.caption(f"{len(tickets)} tickets")
    with st.container(key=key, border=True):
        for ticket in tickets:
            with st.container(key=f"ticket_{ticket['ticket_id']}", border=True):
                render_ticket_card(ticket)
    if tickets:
        styles = []
        for ticket in tickets:
            r, g, b = PRIORITY_COLORS.get(ticket['priority'], PRIORITY_COLORS['medium'])
            styles.append(
                f"div.st-key-ticket_{ticket['ticket_id']} [data-testid='stVerticalBlock'] {{"
                f"background-color: rgba({r}, {g}, {b}, 0.12) !important;"
                f"border-radius: 8px;"
                f"}}"
            )
        st.markdown(f"<style>{''.join(styles)}</style>", unsafe_allow_html=True)

col1, col2, col3 = st.columns(3)
with col1:
    render_column("col_open", "📋 Open", open_tickets)
with col2:
    render_column("col_in_progress", "🔄 In Progress", in_progress_tickets)
with col3:
    render_column("col_resolved", "✅ Resolved", resolved_tickets)

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