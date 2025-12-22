import streamlit as st
import os
import json
from sqlalchemy.orm import Session
from src.database import engine, Document

# NEW IMPORTS: Connect the UI to the backend scripts we just made
from src.gemini_processor import extract_metadata_from_pdf, extract_toc_from_pdf
from src.wikibase_importer import import_book_to_wikibase

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
        # Default value helps testing; remove in production if preferred
        cr_pages = st.text_input("Page Range (e.g. 1-2)", help="For Title, Publisher, Date, ISBN")
    
    with c_toc:
        st.subheader("📑 Table of Contents")
        toc_pages = st.text_input("Page Range (e.g. 5-8)", help="For Chapter mapping")

    st.markdown("---")

    if st.button("🚀 Send to Gemini", type="primary"):
        if not cr_pages and not toc_pages:
            st.error("Please enter at least one page range.")
        else:
            # 1. Show User Feedback
            with st.spinner("🚀 Gemini is analyzing PDF pages... this may take a moment"):
                
                # 2. Run Copyright Extraction
                if cr_pages:
                    try:
                        meta_result = extract_metadata_from_pdf(file_path, cr_pages)
                        # Store as formatted JSON string for the text area
                        st.session_state["meta_json"] = json.dumps(meta_result, indent=4)
                    except Exception as e:
                        st.error(f"Copyright Extraction Failed: {e}")
                        st.session_state["meta_json"] = "{}"

                # 3. Run TOC Extraction
                if toc_pages:
                    try:
                        toc_result = extract_toc_from_pdf(file_path, toc_pages)
                        st.session_state["toc_text"] = toc_result
                    except Exception as e:
                        st.error(f"TOC Extraction Failed: {e}")
                        st.session_state["toc_text"] = ""
            
                # 4. Advance Stage
                st.session_state.pipeline_stage = "proof"
                st.rerun()

# --- 5. STAGE 2: PROOFREADING ---
elif st.session_state.pipeline_stage == "proof":
    st.header("Step 2: Proofread & Structure")
    
    t1, t2 = st.tabs(["©️ Metadata (JSON)", "📑 Table of Contents (Wikitext)"])
    
    with t1:
        st.caption("Edit the JSON below. This will be sent to Wikibase.")
        meta_input = st.text_area("Metadata JSON", 
                                  value=st.session_state.get("meta_json", "{}"), 
                                  height=400, 
                                  key="meta_editor")
        
    with t2:
        st.caption("Edit the Wikitext below. This will be used for page splitting.")
        toc_input = st.text_area("TOC Wikitext", 
                                 value=st.session_state.get("toc_text", ""), 
                                 height=600, 
                                 key="toc_editor")

    c_act1, c_act2 = st.columns([1, 4])
    
    with c_act1:
        if st.button("⬅️ Back"):
            st.session_state.pipeline_stage = "setup"
            st.rerun()
            
    with c_act2:
        if st.button("✅ Approve & Import to Wikibase", type="primary"):
            try:
                # Parse JSON from text area
                data = json.loads(meta_input)
                
                with st.spinner("Connecting to Wikibase..."):
                    # Call the importer
                    new_id = import_book_to_wikibase(data)
                    
                st.success(f"Successfully created Item: {new_id}")
                st.balloons()
                
                # TODO: Save the TOC to the database or file for the next step (Splitting)
                
            except json.JSONDecodeError:
                st.error("Invalid JSON in Metadata tab. Please fix formatting.")
            except Exception as e:
                st.error(f"Import Failed: {e}")
