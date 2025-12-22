import streamlit as st
import os
from sqlalchemy.orm import Session
from src.database import engine, Document

st.set_page_config(layout="wide", page_title="Book Pipeline")

# --- Helper: Navigation ---
def go_back():
    st.switch_page("app.py")

# --- 1. Load Context ---
if "selected_doc_id" not in st.session_state or not st.session_state.selected_doc_id:
    st.warning("No document selected.")
    st.button("⬅️ Return to Dashboard", on_click=go_back)
    st.stop()

doc_id = st.session_state.selected_doc_id

with Session(engine) as session:
    record = session.get(Document, doc_id)
    if not record:
        st.error(f"Document {doc_id} not found in database.")
        st.stop()
    
    filename = record.filename
    file_path = record.file_path
    txt_path = file_path.replace(".pdf", ".txt")
    has_txt = os.path.exists(txt_path)

# --- 2. Pipeline State Management ---
if "pipeline_stage" not in st.session_state:
    st.session_state.pipeline_stage = "setup" 

# --- 3. UI: Header ---
c1, c2 = st.columns([3, 1])
with c1:
    st.title(f"📖 Processing: {filename}")
with c2:
    if st.button("⬅️ Back to Dashboard", use_container_width=True):
        go_back()

if not has_txt:
    st.error(f"❌ Critical: No OCR text file found at {txt_path}. Please run batch_factory.py first.")
    st.stop()
else:
    st.success(f"✅ Base OCR Text Found ({os.path.getsize(txt_path)/1024:.1f} KB)", icon="💾")

st.divider()

# --- 4. STAGE 1: SETUP & SELECTION ---
if st.session_state.pipeline_stage == "setup":
    st.header("Step 1: Define Key Sections")
    st.info("Enter the page ranges (from your PDF viewer) for high-quality Gemini extraction.")

    c_cr, c_toc = st.columns(2)
    with c_cr:
        st.subheader("©️ Copyright Info")
        cr_pages = st.text_input("Page Range (e.g. 1-2)", help="For Title, Publisher, Date, ISBN")
    
    with c_toc:
        st.subheader("📑 Table of Contents")
        toc_pages = st.text_input("Page Range (e.g. 5-8)", help="For Chapter mapping")

    st.markdown("---")

    if st.button("🚀 Send to Gemini", type="primary"):
        if not cr_pages and not toc_pages:
            st.error("Please enter at least one page range.")
        else:
            # Store ranges in session
            st.session_state["cr_range"] = cr_pages
            st.session_state["toc_range"] = toc_pages
            
            # TODO: trigger_gemini_extraction(file_path, cr_pages, toc_pages)
            
            # Advance stage
            st.session_state.pipeline_stage = "proof"
            st.rerun()

# --- 5. STAGE 2: PROOFREADING (Placeholder) ---
elif st.session_state.pipeline_stage == "proof":
    st.header("Step 2: Proofread & Structure")
    
    t1, t2 = st.tabs(["©️ Metadata (JSON/CSV)", "📑 Table of Contents (Wikitext)"])
    
    with t1:
        st.caption("Gemini Output for Copyright Pages")
        st.text_area("Metadata", height=400, key="meta_editor")
        
    with t2:
        st.caption("Gemini Output for Table of Contents")
        st.text_area("TOC Wikitext", height=600, key="toc_editor")

    if st.button("✅ Approve & Import"):
        # TODO: trigger_final_import()
        st.success("Import logic will go here.")
