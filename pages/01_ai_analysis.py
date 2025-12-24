import streamlit as st
import pandas as pd
import os
import platform
import subprocess
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

# Local imports
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document

# --- Configuration ---
st.set_page_config(page_title="AI Analyst", layout="wide")

st.title("🤖 AI Analyst")

# --- Helper Functions ---
def open_local_file(path):
    """Platform-independent file opener."""
    if os.path.exists(path):
        try:
            if platform.system() == "Linux":
                subprocess.call(["xdg-open", path])
            elif platform.system() == "Darwin":
                subprocess.call(["open", path])
            elif platform.system() == "Windows":
                os.startfile(path)
        except Exception as e:
            st.error(f"Error opening file: {e}")
    else:
        st.error("File not found on disk.")

def get_analysis_queue():
    """Fetch documents that need analysis (Pending or Evaluated)."""
    with Session(engine) as session:
        # Sort: PENDING first, then by ID
        stm = select(Document).where(
            Document.status != 'COMPLETED'
        ).order_by(
            Document.status.desc(), # PENDING > EVALUATED
            Document.id
        )
        return session.scalars(stm).all()

def render_sidebar_details(doc):
    st.sidebar.header("📄 File Details")
    st.sidebar.write(f"**Filename:** {doc.filename}")
    st.sidebar.caption(f"ID: {doc.id}")
    
    c1, c2 = st.sidebar.columns(2)
    with c1:
        if st.sidebar.button("📄 Open", key="sb_open"):
            open_local_file(doc.file_path)
    with c2:
        if st.sidebar.button("📂 Folder", key="sb_fold"):
            open_local_file(os.path.dirname(doc.file_path))
    
    st.sidebar.divider()

# --- Main Interface ---

# 1. Fetch Data
docs = get_analysis_queue()
pending_docs = [d for d in docs if d.status == 'PENDING']
queue_count = len(docs)
pending_count = len(pending_docs)

# 2. Queue Table
st.subheader(f"Analysis Queue ({pending_count} Pending / {queue_count} Total)")

# Display only top 50 to keep UI fast
display_docs = docs[:50]

queue_data = [{
    "ID": d.id,
    "Filename": d.filename,
    "Status": d.status,
    "Score": d.priority_score,
    "Language": d.language
} for d in display_docs]

selected_doc_id = None

event = st.dataframe(
    queue_data,
    column_order=["ID", "Filename", "Status", "Score", "Language"],
    width="stretch",
    hide_index=True,
    selection_mode="single-row",
    on_select="rerun",
    key="ai_queue_table"
)

if len(event.selection['rows']) > 0:
    idx = event.selection['rows'][0]
    selected_doc_id = queue_data[idx]["ID"]

st.divider()

# 3. Work Area
if selected_doc_id:
    # Single Document Mode
    with Session(engine) as session:
        record = session.get(Document, selected_doc_id)
        render_sidebar_details(record)
        
        st.header(f"Analyzing: {record.filename}")
        
        col_left, col_right = st.columns([1, 1])
        
        with col_left:
            st.subheader("Current Metadata")
            if pd.notna(record.priority_score):
                st.metric("Priority Score", f"{record.priority_score}/10")
                st.write(f"**Language:** {record.language}")
                st.info(f"**Summary:** {record.summary}")
                with st.expander("Justification"):
                    st.caption(record.ai_justification)
            else:
                st.warning("Status: Pending Analysis")

        with col_right:
            st.subheader("Actions")
            
            # Action: RUN AI
            if st.button("✨ Run AI Evaluation", type="primary", use_container_width=True):
                with st.spinner("Reading PDF & Querying LLM..."):
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
                        st.error("Could not extract images from PDF.")

            st.markdown("---")
            
            # Action: MANUAL OVERRIDE
            with st.form("manual_override"):
                st.write("**Manual Override**")
                new_score = st.number_input("Set Score (1-10)", min_value=1, max_value=10, value=int(record.priority_score) if record.priority_score else 5)
                new_lang = st.text_input("Language", value=record.language or "")
                
                if st.form_submit_button("💾 Save Manual Data"):
                    record.priority_score = new_score
                    record.language = new_lang
                    if "Manually Overridden" not in (record.ai_justification or ""):
                        record.ai_justification = (record.ai_justification or "") + "\n[Manually Overridden]"
                    session.commit()
                    st.success("Saved.")
                    st.rerun()

else:
    # Bulk Action Mode
    st.info("👈 Select a document from the table to inspect individually.")
    st.markdown("### Bulk Operations")
    
    if pending_count > 0:
        if st.button(f"🚀 Run AI Analysis on ALL Pending ({pending_count} files)", type="primary"):
            progress_bar = st.progress(0, text="Starting Batch Job...")
            status_text = st.empty()
            
            # Re-fetch strictly pending docs to be safe
            with Session(engine) as session:
                # We fetch IDs first to avoid keeping session open too long
                p_stm = select(Document.id).where(Document.status == 'PENDING')
                p_ids = session.scalars(p_stm).all()
                
                total = len(p_ids)
                success_count = 0
                
                for i, doc_id in enumerate(p_ids):
                    # Process one by one
                    doc = session.get(Document, doc_id)
                    status_text.write(f"Processing ({i+1}/{total}): {doc.filename}...")
                    
                    try:
                        images = extract_preview_images(doc.file_path)
                        if images:
                            result = evaluate_document(images)
                            if result:
                                doc.priority_score = result['priority_score']
                                doc.summary = result['summary']
                                doc.language = result['language']
                                doc.ai_justification = result['ai_justification']
                                doc.status = "EVALUATED"
                                session.commit()
                                success_count += 1
                    except Exception as e:
                        print(f"Failed on {doc_id}: {e}")
                        # Continue to next file even if one fails
                        continue
                    
                    progress_bar.progress((i + 1) / total)
            
            st.success(f"Batch Complete! Successfully analyzed {success_count} documents.")
            st.rerun()
    else:
        st.success("🎉 No pending documents! Queue is clear.")
