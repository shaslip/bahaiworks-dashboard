import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

# Local imports
from src.database import engine, Document

# --- Configuration ---
st.set_page_config(
    page_title="Bahai.works Digitization Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Helper Functions ---
def load_data():
    """Fetches all documents from the database into a Pandas DataFrame."""
    with Session(engine) as session:
        # Select all fields
        query = select(Document.id, 
                       Document.filename, 
                       Document.status, 
                       Document.priority_score, 
                       Document.language, 
                       Document.summary,
                       Document.file_path)
        
        # Load into DataFrame
        df = pd.read_sql(query, session.bind)
        return df

def get_metrics(df):
    """Calculates basic stats for the top bar."""
    total = len(df)
    pending = len(df[df['status'] == 'PENDING'])
    completed = len(df[df['status'] == 'DIGITIZED'])
    high_priority = len(df[df['priority_score'] >= 8]) if 'priority_score' in df else 0
    return total, pending, completed, high_priority

# --- UI Layout ---
st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics Row
m1, m2, m3, m4 = st.columns(4)
total, pending, completed, high_pri = get_metrics(df)

m1.metric("Total Documents Found", total)
m2.metric("Pending AI Review", pending)
m3.metric("Digitized", completed)
m4.metric("High Priority (>8)", high_pri)

st.markdown("---")

# 3. Main Data View
st.subheader("Document Queue")

# Filter Tabs
tab1, tab2 = st.tabs(["All Files", "High Priority Only"])

with tab1:
    # Main interactive table
    # We hide 'file_path' and 'summary' from the main view to keep it clean
    display_cols = ['id', 'filename', 'status', 'priority_score', 'language']
    
    event = st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun" # Allows us to detect clicks
    )

with tab2:
    if not df.empty and 'priority_score' in df.columns:
        high_pri_df = df[df['priority_score'] >= 8]
        st.dataframe(high_pri_df[display_cols], use_container_width=True, hide_index=True)
    else:
        st.info("No documents evaluated yet.")

# 4. Detail View (Sidebar)
# When a user clicks a row in the dataframe, show details in sidebar
if len(event.selection['rows']) > 0:
    selected_index = event.selection['rows'][0]
    selected_id = df.iloc[selected_index]['id']
    
    # Fetch full details for this specific doc
    record = df[df['id'] == selected_id].iloc[0]
    
    with st.sidebar:
        st.header("📄 Document Details")
        st.write(f"**Filename:** {record['filename']}")
        st.write(f"**Path:** `{record['file_path']}`")
        
        st.divider()
        
        st.subheader("AI Analysis")
        if record['priority_score']:
            st.metric("Priority Score", f"{record['priority_score']}/10")
            st.write(f"**Language:** {record['language']}")
            st.info(f"**Summary:** {record['summary']}")
        else:
            st.warning("Status: Pending Analysis")
            st.write("Click 'Process' to run AI evaluation (Feature coming in Step 3)")

else:
    with st.sidebar:
        st.write("Select a document from the table to view details.")

# 5. Action Buttons (Placeholder)
if st.sidebar.button("Refresh Database"):
    st.rerun()
