import streamlit as st
import pandas as pd
import os
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
def get_analysis_queue():
    """Fetch documents that need analysis (Pending or Evaluated, but not Completed)."""
    with Session(engine) as session:
        # We prioritize PENDING, then EVALUATED (for review), excluding COMPLETED
        stm = select(Document).where(
            Document.status != 'COMPLETED'
        ).order_by(
            Document.status, # PENDING comes before EVALUATED alphabetically? No, 'E' < 'P'. 
            # Let's sort by NULL priority first, then ID
            Document.priority_score.nullsfirst(),
            Document.id
        )
        return session.scalars(stm).all()

def render_sidebar_details(doc):
    """Simple file info for the sidebar."""
    st.sidebar.header("📄 File Details")
    st.sidebar.write(f"**Filename:** {doc.filename}")
    st.sidebar.caption(f"ID: {doc.id}")
    
    c1, c2 = st.sidebar.columns(2)
    with c1:
        if st.button("📄 Open", key="sb_open"):
            # You might need to import your 'open_local_file' helper here or copy it
            pass 
    
    st.sidebar.divider()

# --- Main Interface ---

# 1. Fetch Data
docs = get_analysis_queue()
queue_count = len(docs)
pending_count = len([d for d in docs if d.status == 'PENDING'])

# 2. Queue Table
st.subheader(f"Analysis Queue ({pending_count} Pending / {queue_count} Total)")

# Batch slice for display
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
    # Fetch fresh object
    with Session(engine) as session:
        record = session.get(Document, selected_doc_id)
        
        # Render Sidebar Info
        render_sidebar_details(record)
        
        # --- AI ANALYST WORKSPACE (Migrated from app.py) ---
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
    st.info("👈 Select a document from the table to begin analysis.")
    
    # Optional: Bulk Action
    if pending_count > 0:
        if st.button(f"⚡ Batch Run Next 5 Pending Files"):
            # Batch logic implementation would go here
            st.info("Batch implementation pending.")
