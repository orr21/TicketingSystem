import streamlit as st
import psycopg
import os
import time
from databricks.sdk import WorkspaceClient
from datetime import datetime
import pandas as pd

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
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
                ('Cannot log in to VPN', 'open', 'alice@example.com'),
                ('Billing discrepancy on invoice #2041', 'in_progress', 'bob@example.com'),
                ('Databricks app deploy failing', 'resolved', 'carol@example.com'),
            ]
            ticket_ids = []
            for title, status, created_by in sample_tickets:
                cur.execute(
                    "INSERT INTO tickets (title, status, created_by) VALUES (%s, %s, %s) RETURNING ticket_id",
                    (title, status, created_by),
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
            SELECT ticket_id, title, status, created_by, created_at,
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
                'created_by': row[3],
                'created_at': row[4],
                'message_count': row[5]
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

def create_ticket(title, status='open'):
    """Create a new ticket"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        # Get current user
        current_user = w.current_user.me().user_name
        
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tickets (title, status, created_by)
                VALUES (%s, %s, %s)
                RETURNING ticket_id
            """, (title, status, current_user))
            
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

def render_ticket_card(ticket):
    """Render a ticket card"""
    with st.container(border=True):
        st.markdown(f"**{ticket['title']}**")
        st.caption(f"#{ticket['ticket_id']} • {ticket['message_count']} messages")
        st.caption(f"Created by: {ticket['created_by']}")
        
        if st.button("View Details", key=f"view_{ticket['ticket_id']}", use_container_width=True):
            st.session_state.selected_ticket = ticket['ticket_id']
            st.rerun()

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
            SELECT ticket_id, title, status, created_by, created_at
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
            'created_by': row[3],
            'created_at': row[4]
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

# Debug section (remove after testing)
with st.expander("🔍 Debug: Environment Variables", expanded=False):
    st.write("**Lakebase Resource Env Vars:**")
    lakebase_vars = {k: v for k, v in os.environ.items() if 'LAKEBASE' in k or 'PG' in k or 'POSTGRES' in k}
    if lakebase_vars:
        st.json(lakebase_vars)
    else:
        st.warning("No Lakebase or PostgreSQL environment variables found!")
    
    st.write("**All Environment Variables:**")
    if st.checkbox("Show all env vars"):
        st.json(dict(os.environ))

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
    
    if st.button("Create Ticket", key="create_ticket", type="primary", disabled=not new_ticket_title):
        ticket_id = create_ticket(new_ticket_title, new_ticket_status)
        if ticket_id:
            st.success(f"Ticket #{ticket_id} created successfully!")
            st.rerun()

st.divider()

# Kanban board
st.subheader("Ticket Board")

# Fetch tickets for each status
open_tickets = get_tickets_by_status('open')
in_progress_tickets = get_tickets_by_status('in_progress')
resolved_tickets = get_tickets_by_status('resolved')

# Create three columns for Kanban
col1, col2, col3 = st.columns(3)

with col1:
    st.markdown("### 📋 Open")
    st.caption(f"{len(open_tickets)} tickets")
    for ticket in open_tickets:
        render_ticket_card(ticket)
        
        # Move to In Progress button
        if st.button("→ Move to In Progress", key=f"move_ip_{ticket['ticket_id']}", use_container_width=True):
            if update_ticket_status(ticket['ticket_id'], 'in_progress'):
                st.success("Ticket moved!")
                st.rerun()

with col2:
    st.markdown("### 🔄 In Progress")
    st.caption(f"{len(in_progress_tickets)} tickets")
    for ticket in in_progress_tickets:
        render_ticket_card(ticket)
        
        # Move buttons
        col_left, col_right = st.columns(2)
        with col_left:
            if st.button("← Open", key=f"move_open_{ticket['ticket_id']}", use_container_width=True):
                if update_ticket_status(ticket['ticket_id'], 'open'):
                    st.success("Ticket moved!")
                    st.rerun()
        with col_right:
            if st.button("Resolved →", key=f"move_resolved_{ticket['ticket_id']}", use_container_width=True):
                if update_ticket_status(ticket['ticket_id'], 'resolved'):
                    st.success("Ticket moved!")
                    st.rerun()

with col3:
    st.markdown("### ✅ Resolved")
    st.caption(f"{len(resolved_tickets)} tickets")
    for ticket in resolved_tickets:
        render_ticket_card(ticket)
        
        # Move to In Progress button
        if st.button("← Move to In Progress", key=f"move_ip_from_res_{ticket['ticket_id']}", use_container_width=True):
            if update_ticket_status(ticket['ticket_id'], 'in_progress'):
                st.success("Ticket moved!")
                st.rerun()

# Show modal if a ticket is selected
show_ticket_modal()