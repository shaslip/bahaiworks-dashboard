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
    """
    Fetches all documents.
    Sorts by Priority (High to Low) then Filename (A-Z) to ensure the list 
    doesn't jump around randomly when you reload.
    """
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
    # Handle NaN values safely
    high_priority = len(df[df['priority_score'] >= 8]) if not df.empty else 0
    return total, pending, completed, high_priority

# --- Sidebar Fragment (Isolated Refresh) ---
@st.fragment
def render_sidebar(selected_id):
    """
    Renders the sidebar for a specific document ID.
    Using @st.fragment ensures that clicking buttons here DOES NOT reload the main table.
    """
    with Session(engine) as session:
        # Fetch fresh object from DB
        record = session.get(Document, selected_id)
        
        if not record:
            st.sidebar.error("Document not found in database.")
            return

        with st.sidebar:
            st.header("📄 Document Details")
            st.write(f"**Filename:** {record.filename}")
            
            # --- 1. File Actions (No Page Reload) ---
            b1, b2 = st.columns(2)
            
            with b1:
                if st.button("📄 Open File", use_container_width=True):
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
                if st.button("📂 Open Folder", use_container_width=True):
                    folder_path = os.path.dirname(record.file_path)
                    if os.path.exists(folder_path):
                        try:
                            if platform.system() == "Linux":
                                # Dolphin specific command
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
            
            # Check if score exists (Safe check for None/NaN)
            if pd.notna(record.priority_score):
                st.metric("Priority Score", f"{record.priority_score}/10")
                st.write(f"**Language:** {record.language}")
                st.info(f"**Summary:** {record.summary}")
                
                with st.expander("Justification"):
                    st.caption(record.ai_justification)
                
                st.divider()
                st.subheader("Manual Controls")
                
                # Manual Override
                c1, c2 = st.columns(2)
                with c1:
                    new_score = st.number_input(
                        "Set Score", 
                        min_value=1, 
                        max_value=10, 
                        value=int(record.priority_score), 
                        label_visibility="collapsed"
                    )
                with c2:
                    if st.button("💾 Save Override"):
                        record.priority_score = new_score
                        if "Manually Overridden" not in (record.ai_justification or ""):
                            record.ai_justification = (record.ai_justification or "") + "\n[Manually Overridden]"
                        session.commit()
                        st.toast(f"Score updated to {new_score}")
                        st.rerun() # Must reload to update main table sorting

                # Re-run AI
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

            # --- 3. Pending State ---
            else:
                st.warning("Status: Pending Analysis")
                
                if st.button("✨ Run AI Evaluation", type="primary"):
                    with st.spinner("Extracting pages and reading with Gemini..."):
                        images = extract_preview_images(record.file_path)
                        
                        if not images:
                            st.error("Failed to extract images.")
                        else:
                            result = evaluate_document(images)
                            if result:
                                record.priority_score = result['priority_score']
                                record.summary = result['summary']
                                record.language = result['language']
                                record.ai_justification = result['ai_justification']
                                record.status = "EVALUATED"
                                session.commit()
                                st.success("Analysis Complete!")
                                st.rerun() # Must reload to update main table
                            else:
                                st.error("AI returned no results.")

# --- Main App Execution ---

st.title("📚 Bahai.works Prioritization Engine")

# 1. Load Data
df = load_data()

# 2. Metrics (Restored!)
m1, m2, m3, m4 = st.columns(4)
total, pending, completed, high_pri = get_metrics(df)

m1.metric("Total Documents Found", total)
m2.metric("Pending AI Review", pending)
m3.metric("Digitized", completed)
m4.metric("High Priority (>8)", high_pri)

st.markdown("---")

# 3. Main Interactive Table (Restored!)
st.subheader("Document Queue")

# Initialize Selection State
if "selected_doc_id" not in st.session_state:
    st.session_state.selected_doc_id = None

tab1, tab2 = st.tabs(["All Files", "High Priority Only"])
display_cols = ['id', 'filename', 'status', 'priority_score', 'language']

# Tab 1: All Files
with tab1:
    event = st.dataframe(
        df[display_cols],
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key="main_table"
    )
    if len(event.selection['rows']) > 0:
        idx = event.selection['rows'][0]
        st.session_state.selected_doc_id = int(df.iloc[idx]['id'])

# Tab 2: High Priority
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
        if len(event_hp.selection['rows']) > 0:
            idx = event_hp.selection['rows'][0]
            st.session_state.selected_doc_id = int(high_pri_df.iloc[idx]['id'])
    else:
        st.info("No documents evaluated yet.")

# 4. Render Sidebar (Fragment)
if st.session_state.selected_doc_id is not None:
    render_sidebar(st.session_state.selected_doc_id)
else:
    with st.sidebar:
        st.info("Select a document from the table to view details.")
