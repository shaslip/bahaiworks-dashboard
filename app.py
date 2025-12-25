import streamlit as st
import pandas as pd
import subprocess
import platform
import os
import re
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
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
    pending = len(df[df['status'].isin(['PENDING', 'READY_FOR_OCR'])]) 
    digitized = len(df[df['status'] == 'DIGITIZED'])
    completed = len(df[df['status'] == 'COMPLETED'])
    high_priority = len(df[df['priority_score'] >= 8]) if not df.empty else 0
    return total, pending, digitized, completed, high_priority

def parse_ranges(text):
    """Parses '48-55, 102' into [(48, 55), (102, 102)]"""
    ranges = []
    if not text or not text.strip(): return []
    try:
        parts = text.split(',')
        for p in parts:
            p = p.strip()
            if '-' in p:
                s, e = p.split('-')
                ranges.append((int(s), int(e)))
            elif p.isdigit():
                ranges.append((int(p), int(p)))
    except:
        pass # Fail silently on bad input
    return ranges

# --- Sidebar Fragment ---
@st.fragment
def render_details(selected_id):
    with Session(engine) as session:
        record = session.get(Document, selected_id)
        if not record:
            st.error("Document not found.")
            return

        st.header("📄 Document Details")
        st.write(f"**Filename:** {record.filename}")
        
        # --- File Actions ---
        b1, b2 = st.columns(2)
        with b1:
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
        
        if st.button("✅ Mark as Completed (Remove from Queue)", type="primary"):
            record.status = "COMPLETED"
            session.commit()
            st.success("Document marked as COMPLETED!")
            st.rerun()

# --- Main App Execution ---

st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics
m1, m2, m3, m4, m5 = st.columns(5)
total, pending, digitized, completed, high_pri = get_metrics(df)

m1.metric("Total Documents", total)
m2.metric("Pending AI", pending)
m3.metric("Digitized (Queue)", digitized)
m4.metric("Completed", completed)
m5.metric("High Priority", high_pri)

st.markdown("---")

# 3. Main Interactive Table
st.subheader("Document Queue")

if "selected_doc_id" not in st.session_state:
    st.session_state.selected_doc_id = None

# UPDATED: Added tab4 for "Completed"
tab1, tab2, tab3, tab4, tab5 = st.tabs(["All Files", "High Priority Only", "Ready for OCR", "Digitized", "Completed"])
display_cols = ['id', 'filename', 'status', 'priority_score', 'language']

# --- TAB 1: ALL FILES ---
with tab1:
    # Filter out 'Completed' status so they don't clutter the main view
    filtered_df = df[df['status'] != 'COMPLETED']

    event = st.dataframe(
        filtered_df[display_cols],
        width="stretch",
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key="main_table"
    )
    if len(event.selection['rows']) > 0:
        idx = event.selection['rows'][0]
        st.session_state.selected_doc_id = int(filtered_df.iloc[idx]['id'])

# --- TAB 2: HIGH PRIORITY ---
with tab2:
    if not df.empty and 'priority_score' in df.columns:
        high_pri_df = df[(df['priority_score'] >= 8) & (df['status'] != 'COMPLETED')]
        
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

# --- TAB 3: READY FOR OCR ---
with tab3:
    if not df.empty:
        ready_df = df[df['status'] == 'READY_FOR_OCR']
        
        event_ready = st.dataframe(
            ready_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="ready_table"
        )
        if len(event_ready.selection['rows']) > 0:
            idx = event_ready.selection['rows'][0]
            st.session_state.selected_doc_id = int(ready_df.iloc[idx]['id'])
    else:
        st.info("No documents waiting for OCR.")

# --- TAB 4: DIGITIZED ---
with tab4:
    if not df.empty:
        digitized_df = df[df['status'] == 'DIGITIZED']
        event_dig = st.dataframe(
            digitized_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="dig_table"
        )
        if len(event_dig.selection['rows']) > 0:
            idx = event_dig.selection['rows'][0]
            st.session_state.selected_doc_id = int(digitized_df.iloc[idx]['id'])
    else:
        st.info("No digitized documents found.")

# --- TAB 5: COMPLETED ---
with tab5:
    if not df.empty:
        completed_df = df[df['status'] == 'COMPLETED']
        event_comp = st.dataframe(
            completed_df[display_cols], 
            width="stretch", 
            hide_index=True,
            selection_mode="single-row",
            on_select="rerun",
            key="comp_table"
        )
        if len(event_comp.selection['rows']) > 0:
            idx = event_comp.selection['rows'][0]
            st.session_state.selected_doc_id = int(completed_df.iloc[idx]['id'])
    else:
        st.info("No completed documents yet.")

# 4. Render Sidebar (Caller)
with st.sidebar:
    # Existing code for document details...
    if st.session_state.selected_doc_id is not None:
        render_details(st.session_state.selected_doc_id)
    else:
        st.info("Select a document from the table to view details.")
