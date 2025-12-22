import streamlit as st
import os
import json
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.gemini_processor import extract_metadata_from_pdf, extract_toc_from_pdf
from src.wikibase_importer import import_book_to_wikibase

st.set_page_config(layout="wide", page_title="Book Pipeline")

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
        st.error("Document not found.")
        st.stop()
    
    filename = record.filename
    file_path = record.file_path
    # Guess a default target page title from filename (strip extension, replace _ with space)
    default_target = os.path.splitext(filename)[0].replace("_", " ")
    txt_path = file_path.replace(".pdf", ".txt")
    has_txt = os.path.exists(txt_path)

if "pipeline_stage" not in st.session_state:
    st.session_state.pipeline_stage = "setup" 

# --- UI Header ---
c1, c2 = st.columns([3, 1])
with c1:
    st.title(f"📖 Processing: {filename}")
with c2:
    if st.button("⬅️ Back to Dashboard"): go_back()

if not has_txt:
    st.error(f"❌ Critical: No OCR text file found at {txt_path}.")
    st.stop()

st.divider()

# --- STAGE 1: SETUP ---
if st.session_state.pipeline_stage == "setup":
    st.header("Step 1: Define Targets & Source")
    
    # 1. Target Page Definition
    st.subheader("📍 Target")
    target_page = st.text_input("Bahai.works Page Title", value=default_target, help="Where will the main book page live?")

    # 2. Source Ranges
    st.subheader("📄 Page Ranges (from PDF)")
    c_cr, c_toc = st.columns(2)
    with c_cr:
        cr_pages = st.text_input("Copyright Pages", placeholder="e.g. 1-2")
    with c_toc:
        toc_pages = st.text_input("TOC Pages", placeholder="e.g. 5-8")

    st.markdown("---")

    if st.button("🚀 Send to Gemini", type="primary"):
        if not target_page:
            st.error("Please define a Target Page Title.")
        else:
            st.session_state["target_page"] = target_page
            
            with st.spinner("🤖 Gemini is extracting metadata & TOC structure..."):
                # Run Metadata Extraction
                if cr_pages:
                    res = extract_metadata_from_pdf(file_path, cr_pages)
                    if "error" in res:
                        st.error(f"Metadata Extraction Failed: {res['error']}")
                        if "raw" in res: st.expander("Raw Response").code(res["raw"])
                    
                    st.session_state["meta_result"] = res
                    st.session_state["meta_json_str"] = json.dumps(res.get("data", {}), indent=4)

                # Run TOC Extraction
                if toc_pages:
                    res = extract_toc_from_pdf(file_path, toc_pages)
                    
                    if res.get("error"):
                        st.error(f"TOC Extraction Failed: {res['error']}")
                        if "raw" in res: st.expander("Raw Response").code(res["raw"])
                    
                    st.session_state["toc_result"] = res
                    st.session_state["toc_json_str"] = json.dumps(res.get("toc_json", []), indent=4)
                    st.session_state["toc_wikitext_part"] = res.get("toc_wikitext", "")
                
                # Only advance if we have at least partial success or the user wants to force it
                if not st.session_state.get("meta_result", {}).get("error") and not st.session_state.get("toc_result", {}).get("error"):
                    st.session_state.pipeline_stage = "proof"
                    st.rerun()
                else:
                    st.warning("Errors occurred. See above. Fix ranges or try again.")
                    # Optional: Add a 'Force Continue' button if they want to proceed manually
                    if st.button("Force Continue"):
                        st.session_state.pipeline_stage = "proof"
                        st.rerun()

# --- STAGE 2: PROOFREAD ---
elif st.session_state.pipeline_stage == "proof":
    st.header("Step 2: Proofread & Import")
    
    # Retrieve Data
    full_meta = st.session_state.get("meta_result", {})
    meta_data = full_meta.get("data", {}) # The 'data' sub-object
    
    # Construct the Template Block (Empty fields as requested)
    header_template = f"""{{{{restricted use|where=|until=}}}}
{{{{header
 | title      = 
 | author     = 
 | translator = 
 | compiler   = 
 | section    = 
 | previous   = 
 | next       = 
 | publisher  = 
 | year       = 
 | notes      = 
 | categories = All publications/Books
 | portal     = 
}}}}
{{{{book
 | color = 656258
 | image = 
 | downloads = 
 | translations = 
 | pages = 
 | links = 
}}}}

===Contents===
"""
    # Combine Template + Gemini's List
    default_full_page = header_template + st.session_state.get("toc_wikitext_part", "")

    t1, t2 = st.tabs(["1. Metadata & Copyright", "2. Main Page (TOC)"])
    
    # --- TAB 1: Metadata ---
    with t1:
        c_talk, c_json = st.columns(2)
        with c_talk:
            st.subheader("Talk Page Text")
            st.caption("Clean OCR for legal/copyright reference")
            talk_input = st.text_area("Clean OCR", value=full_meta.get("copyright_text", ""), height=500, key="talk_editor")
        with c_json:
            st.subheader("Wikibase Data (JSON)")
            st.caption("This data creates the Q-Item (Source of Truth)")
            json_input = st.text_area("Metadata", value=json.dumps(meta_data, indent=4), height=500, key="meta_editor")

    # --- TAB 2: TOC ---
    with t2:
        c_toc_json, c_toc_wiki = st.columns(2)
        with c_toc_json:
            st.subheader("Chapter Data (JSON)")
            st.caption("For future scholarly articles processing")
            toc_json_input = st.text_area("Chapters", value=st.session_state.get("toc_json_str", "[]"), height=600, key="toc_json_editor")
            
        with c_toc_wiki:
            st.subheader("Main Page Source")
            
            # --- FIX: Re-introduced Target Page Input Here ---
            target_page_input = st.text_input("Target Page Title", 
                                              value=st.session_state.get("target_page", ""),
                                              key="final_target_input")
            
            # Update session state if user changes it here
            if target_page_input != st.session_state.get("target_page"):
                st.session_state["target_page"] = target_page_input

            st.caption(f"Will create: {target_page_input}")
            # -------------------------------------------------

            full_page_input = st.text_area("Wikitext", value=default_full_page, height=530, key="full_page_editor")

    st.divider()
    
    # --- Actions ---
    c_back, c_approve = st.columns([1, 4])
    with c_back:
        if st.button("⬅️ Back"):
            st.session_state.pipeline_stage = "setup"
            st.rerun()
            
    with c_approve:
        if st.button("✅ Approve All & Import", type="primary"):
            try:
                # 1. Wikibase Import
                wb_data = json.loads(json_input)
                new_qid = None
                with st.spinner("Creating Wikibase Item..."):
                    new_qid = import_book_to_wikibase(wb_data)
                    st.toast(f"Created Item: {new_qid}")
                
                # 2. Bahai.works Page Creation (Placeholder)
                target = st.session_state.get("target_page")
                # upload_to_wiki(page_title=target, content=full_page_input)
                st.success(f"Prepared Page '{target}'")
                
                # 3. Bahai.works Talk Page Creation (Placeholder)
                # upload_to_wiki(page_title=f"Talk:{target}", content=talk_input)
                st.info(f"Prepared Talk Page 'Talk:{target}'")
                
                st.balloons()
                st.session_state["final_qid"] = new_qid
                
                # Move to next stage (Splitting)
                # st.session_state.pipeline_stage = "split"
                # st.rerun()
                
            except Exception as e:
                st.error(f"Import Failed: {e}")
