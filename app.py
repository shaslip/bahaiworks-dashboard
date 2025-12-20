import subprocess
import platform
import os
from src.processor import extract_preview_images
from src.evaluator import evaluate_document
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
    """Fetches all documents sorted by Priority Score (Highest to Lowest)."""
    with Session(engine) as session:
        query = select(Document.id, 
                       Document.filename, 
                       Document.status, 
                       Document.priority_score, 
                       Document.language, 
                       Document.summary,
                       Document.ai_justification,
                       Document.file_path)\
                .order_by(desc(Document.priority_score))
        
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

# Initialize Session State for Selection if it doesn't exist
if "selected_doc_id" not in st.session_state:
    st.session_state.selected_doc_id = None

tab1, tab2 = st.tabs(["All Files", "High Priority Only"])

# We define the dataframe first so we can reference it below
display_cols = ['id', 'filename', 'status', 'priority_score', 'language']

# Determine which dataframe to show based on the active tab
# Note: Streamlit tabs don't stop code execution, so we filter based on logic
active_df = df # Default to all
if tab2._active: # This is an internal check, but for robustness we can just render the table conditionally
    pass 

with tab1:
    event = st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key="main_table" # key helps Streamlit track state
    )

    # CAPTURE SELECTION: If user clicked a row, update session state immediately
    if len(event.selection['rows']) > 0:
        selected_index = event.selection['rows'][0]
        # Map the visual row index back to the actual Database ID
        clicked_id = int(df.iloc[selected_index]['id'])
        st.session_state.selected_doc_id = clicked_id

with tab2:
    if not df.empty and 'priority_score' in df.columns:
        high_pri_df = df[df['priority_score'] >= 8]
        event_hp = st.dataframe(
            high_pri_df[display_cols], 
            use_container_width=True, 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="hp_table"
        )
        
        # Capture selection for this tab too
        if len(event_hp.selection['rows']) > 0:
            selected_index = event_hp.selection['rows'][0]
            clicked_id = int(high_pri_df.iloc[selected_index]['id'])
            st.session_state.selected_doc_id = clicked_id
    else:
        st.info("No documents evaluated yet.")

# 4. Detail View (Sidebar)
# We now render based on session_state, NOT the transient 'event' variable.
# This ensures the sidebar survives button clicks (re-runs).

if st.session_state.selected_doc_id is not None:
    # Verify the selected ID still exists in our database (in case of deletion/filter)
    # We filter the master 'df' to find the record
    selected_record = df[df['id'] == st.session_state.selected_doc_id]

    if not selected_record.empty:
        record = selected_record.iloc[0]
        
        with st.sidebar:
            st.header("📄 Document Details")
            st.write(f"**Filename:** {record['filename']}")
            
            # --- Open File / Folder Buttons ---
            b1, b2 = st.columns(2)
            with b1:
                if st.button("📄 Open File", use_container_width=True):
                    if os.path.exists(record['file_path']):
                        try:
                            if platform.system() == "Linux":
                                subprocess.call(["xdg-open", record['file_path']])
                            elif platform.system() == "Darwin":
                                subprocess.call(["open", record['file_path']])
                            elif platform.system() == "Windows":
                                os.startfile(record['file_path'])
                        except Exception as e:
                            st.error(f"Error: {e}")
                    else:
                        st.error("File not found!")

            with b2:
                if st.button("📂 Open Folder", use_container_width=True):
                    folder_path = os.path.dirname(record['file_path'])
                    if os.path.exists(folder_path):
                        try:
                            if platform.system() == "Linux":
                                subprocess.call(["dolphin", "--select", record['file_path']])
                            elif platform.system() == "Darwin":
                                subprocess.call(["open", "-R", record['file_path']])
                            elif platform.system() == "Windows":
                                subprocess.Popen(f'explorer /select,"{record["file_path"]}"')
                        except Exception as e:
                            st.error(f"Error: {e}")
                    else:
                        st.error("Folder not found!")

            st.divider()
            
            st.subheader("AI Analysis")
            
            # --- ALREADY EVALUATED ---
            if record['priority_score'] is not None:
                st.metric("Priority Score", f"{record['priority_score']}/10")
                st.write(f"**Language:** {record['language']}")
                st.info(f"**Summary:** {record['summary']}")
                
                with st.expander("See AI Justification"):
                    st.caption(record['ai_justification'])
                
                st.divider()

                # Manual Override
                st.subheader("Manual Controls")
                c1, c2 = st.columns(2)
                with c1:
                    new_score = st.number_input(
                        "Set Score", min_value=1, max_value=10, 
                        value=int(record['priority_score']), 
                        label_visibility="collapsed"
                    )
                with c2:
                    if st.button("💾 Save"):
                        with Session(engine) as session:
                            # Must fetch fresh object to write
                            doc_to_update = session.get(Document, int(record['id']))
                            doc_to_update.priority_score = new_score
                            if "Manually Overridden" not in (doc_to_update.ai_justification or ""):
                                doc_to_update.ai_justification = (doc_to_update.ai_justification or "") + "\n[Manually Overridden]"
                            session.commit()
                        st.toast(f"Score updated to {new_score}")
                        st.rerun()

                if st.button("🔄 Re-run AI Evaluation"):
                    with st.spinner("Re-processing..."):
                        images = extract_preview_images(record['file_path'])
                        if images:
                            result = evaluate_document(images)
                            if result:
                                with Session(engine) as session:
                                    doc_to_update = session.get(Document, int(record['id']))
                                    doc_to_update.priority_score = result['priority_score']
                                    doc_to_update.summary = result['summary']
                                    doc_to_update.language = result['language']
                                    doc_to_update.ai_justification = result['ai_justification']
                                    doc_to_update.status = "EVALUATED"
                                    session.commit()
                                st.success("Updated!")
                                st.rerun()
                            else:
                                st.error("AI failed.")
                        else:
                            st.error("Image extraction failed.")

            # --- PENDING ANALYSIS ---
            else:
                st.warning("Status: Pending Analysis")
                if st.button("✨ Run AI Evaluation", type="primary"):
                    with st.spinner("Analyzing..."):
                        images = extract_preview_images(record['file_path'])
                        if images:
                            result = evaluate_document(images)
                            if result:
                                with Session(engine) as session:
                                    doc_to_update = session.get(Document, int(record['id']))
                                    doc_to_update.priority_score = result['priority_score']
                                    doc_to_update.summary = result['summary']
                                    doc_to_update.language = result['language']
                                    doc_to_update.ai_justification = result['ai_justification']
                                    doc_to_update.status = "EVALUATED"
                                    session.commit()
                                st.success("Done!")
                                st.rerun()
                            else:
                                st.error("AI failed.")
                        else:
                            st.error("Image extraction failed.")
            
            # Close Button
            if st.button("Close Sidebar"):
                st.session_state.selected_doc_id = None
                st.rerun()

    else:
        st.session_state.selected_doc_id = None # ID no longer exists
else:
    with st.sidebar:
        st.info("Select a document from the table to view details.")

# 5. Action Buttons (Placeholder)
if st.sidebar.button("Refresh Database"):
    st.rerun()
