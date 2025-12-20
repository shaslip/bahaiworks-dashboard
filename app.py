import streamlit as st
import pandas as pd
import subprocess
import platform
import os
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

# Local imports
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document

# --- Configuration ---
st.set_page_config(
    page_title="Bahai.works Digitization Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Helper Functions ---
def load_data():
    """Fetches all documents with stable sorting."""
    with Session(engine) as session:
        query = select(Document.id, 
                       Document.filename, 
                       Document.status, 
                       Document.priority_score, 
                       Document.language, 
                       Document.summary,
                       Document.ai_justification,
                       Document.file_path)\
                .order_by(
                    desc(Document.priority_score), 
                    Document.filename
                )
        df = pd.read_sql(query, session.bind)
        return df

def get_metrics(df):
    total = len(df)
    pending = len(df[df['status'] == 'PENDING'])
    completed = len(df[df['status'] == 'DIGITIZED'])
    high_priority = len(df[df['priority_score'] >= 8]) if not df.empty else 0
    return total, pending, completed, high_priority

# --- Sidebar Fragment ---
# FIX 1: Removed 'with st.sidebar' context from INSIDE this function to prevent crash.
# This function now generates generic UI elements, which we place in the sidebar later.
@st.fragment
def render_details(selected_id):
    with Session(engine) as session:
        record = session.get(Document, selected_id)
        
        if not record:
            st.error("Document not found.")
            return

        # Note: We use st.header, st.write directly (not st.sidebar.header)
        # The placement is determined by where this function is called.
        st.header("📄 Document Details")
        st.write(f"**Filename:** {record.filename}")
        
        # --- 1. File Actions ---
        b1, b2 = st.columns(2)
        with b1:
            # FIX 2: Replaced use_container_width=True with width="stretch" per logs
            if st.button("📄 Open File", width="stretch"):
                if os.path.exists(record.file_path):
                    try:
                        if platform.system() == "Linux":
                            subprocess.call(["xdg-open", record.file_path])
                        elif platform.system() == "Darwin":
                            subprocess.call(["open", record.file_path])
                        elif platform.system() == "Windows":
                            os.startfile(record.file_path)
                    except Exception as e:
                        st.error(f"Error: {e}")
                else:
                    st.error("File not found!")

        with b2:
            if st.button("📂 Open Folder", width="stretch"):
                folder_path = os.path.dirname(record.file_path)
                if os.path.exists(folder_path):
                    try:
                        if platform.system() == "Linux":
                            subprocess.call(["dolphin", "--select", record.file_path])
                        elif platform.system() == "Darwin":
                            subprocess.call(["open", "-R", record.file_path])
                        elif platform.system() == "Windows":
                            subprocess.Popen(f'explorer /select,"{record.file_path}"')
                    except Exception as e:
                        st.error(f"Error: {e}")
                else:
                    st.error("Folder not found!")

        st.divider()
        
        # --- 2. AI & Data Analysis ---
        st.subheader("AI Analysis")
        
        if pd.notna(record.priority_score):
            st.metric("Priority Score", f"{record.priority_score}/10")
            st.write(f"**Language:** {record.language}")
            st.info(f"**Summary:** {record.summary}")
            
            with st.expander("Justification"):
                st.caption(record.ai_justification)
            
            st.divider()
            st.subheader("Manual Controls")
            
            c1, c2 = st.columns(2)
            with c1:
                new_score = st.number_input(
                    "Set Score", min_value=1, max_value=10, 
                    value=int(record.priority_score), label_visibility="collapsed"
                )
            with c2:
                if st.button("💾 Save Override"):
                    record.priority_score = new_score
                    if "Manually Overridden" not in (record.ai_justification or ""):
                        record.ai_justification = (record.ai_justification or "") + "\n[Manually Overridden]"
                    session.commit()
                    st.toast(f"Score updated to {new_score}")
                    st.rerun()

            if st.button("🔄 Re-run AI Evaluation"):
                with st.spinner("Re-processing..."):
                    images = extract_preview_images(record.file_path)
                    if images:
                        result = evaluate_document(images)
                        if result:
                            record.priority_score = result['priority_score']
                            record.summary = result['summary']
                            record.language = result['language']
                            record.ai_justification = result['ai_justification']
                            record.status = "EVALUATED"
                            session.commit()
                            st.success("Updated!")
                            st.rerun()
                        else:
                            st.error("AI returned no results.")
                    else:
                        st.error("Image extraction failed.")

        else:
            st.warning("Status: Pending Analysis")
            if st.button("✨ Run AI Evaluation", type="primary"):
                with st.spinner("Analyzing..."):
                    images = extract_preview_images(record.file_path)
                    if images:
                        result = evaluate_document(images)
                        if result:
                            record.priority_score = result['priority_score']
                            record.summary = result['summary']
                            record.language = result['language']
                            record.ai_justification = result['ai_justification']
                            record.status = "EVALUATED"
                            session.commit()
                            st.success("Analysis Complete!")
                            st.rerun()
                        else:
                            st.error("AI returned no results.")

# --- Main App Execution ---

st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics
m1, m2, m3, m4 = st.columns(4)
total, pending, completed, high_pri = get_metrics(df)
m1.metric("Total Documents Found", total)
m2.metric("Pending AI Review", pending)
m3.metric("Digitized", completed)
m4.metric("High Priority (>8)", high_pri)

st.markdown("---")

# 3. Main Interactive Table
st.subheader("Document Queue")

if "selected_doc_id" not in st.session_state:
    st.session_state.selected_doc_id = None

tab1, tab2 = st.tabs(["All Files", "High Priority Only"])
display_cols = ['id', 'filename', 'status', 'priority_score', 'language']

with tab1:
    # FIX 2: Replaced use_container_width=True with width="stretch"
    event = st.dataframe(
        df[display_cols],
        width="stretch",
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key="main_table"
    )
    if len(event.selection['rows']) > 0:
        idx = event.selection['rows'][0]
        st.session_state.selected_doc_id = int(df.iloc[idx]['id'])

with tab2:
    if not df.empty and 'priority_score' in df.columns:
        high_pri_df = df[df['priority_score'] >= 8]
        event_hp = st.dataframe(
            high_pri_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="hp_table"
        )
        if len(event_hp.selection['rows']) > 0:
            idx = event_hp.selection['rows'][0]
            st.session_state.selected_doc_id = int(high_pri_df.iloc[idx]['id'])
    else:
        st.info("No documents evaluated yet.")

# 4. Render Sidebar (Caller)
with st.sidebar:
    if st.session_state.selected_doc_id is not None:
        # We call the fragment function INSIDE the sidebar context
        render_details(st.session_state.selected_doc_id)
    else:
        st.info("Select a document from the table to view details.")
