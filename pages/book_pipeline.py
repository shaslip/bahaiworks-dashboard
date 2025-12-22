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
    
    # Default Target Guess
    if "target_page" not in st.session_state:
        st.session_state["target_page"] = os.path.splitext(filename)[0].replace("_", " ")
    
    txt_path = file_path.replace(".pdf", ".txt")
    has_txt = os.path.exists(txt_path)

if "pipeline_stage" not in st.session_state:
    st.session_state.pipeline_stage = "setup" 

# --- UI Header & Global Settings ---
c1, c2 = st.columns([3, 1])
with c1:
    st.title(f"📖 {filename}")
    # GLOBAL TARGET INPUT
    target_page = st.text_input("🎯 Bahai.works Page Title", 
                                value=st.session_state["target_page"],
                                key="global_target_input")
    # Sync back to session state immediately
    st.session_state["target_page"] = target_page

with c2:
    if st.button("⬅️ Back to Dashboard"): go_back()

if not has_txt:
    st.error(f"❌ Critical: No OCR text file found at {txt_path}.")
    st.stop()

st.divider()

# --- STAGE 1: SETUP ---
if st.session_state.pipeline_stage == "setup":
    st.info("Select page ranges to extract high-quality metadata and structure.")
    
    c_cr, c_toc = st.columns(2)
    with c_cr:
        st.subheader("©️ Copyright Pages")
        cr_pages = st.text_input("Range (e.g. 1-2)", key="cr_input")
    with c_toc:
        st.subheader("📑 TOC Pages")
        toc_pages = st.text_input("Range (e.g. 5-8)", key="toc_input")

    st.markdown("---")

    if st.button("🚀 Send to Gemini", type="primary"):
        # We don't need to check target_page here anymore, it's global
        with st.spinner("🤖 Gemini is extracting..."):
            
            # Run Metadata
            if cr_pages:
                res = extract_metadata_from_pdf(file_path, cr_pages)
                if "error" in res:
                    st.error(f"Meta Error: {res['error']}")
                else:
                    st.session_state["meta_result"] = res
                    # Store clean strings for editors
                    st.session_state["talk_text"] = res.get("copyright_text", "")
                    st.session_state["meta_json_str"] = json.dumps(res.get("data", {}), indent=4)

            # Run TOC
            if toc_pages:
                res = extract_toc_from_pdf(file_path, toc_pages)
                if res.get("error"):
                    st.error(f"TOC Error: {res['error']}")
                else:
                    st.session_state["toc_result"] = res
                    st.session_state["toc_json_str"] = json.dumps(res.get("toc_json", []), indent=4)
                    st.session_state["toc_wikitext_part"] = res.get("toc_wikitext", "")
            
            # Advance
            st.session_state.pipeline_stage = "proof"
            st.rerun()

# --- STAGE 2: PROOFREAD & IMPORT ---
elif st.session_state.pipeline_stage == "proof":
    
    # Template Construction
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
    default_full_page = header_template + st.session_state.get("toc_wikitext_part", "")

    # TABS
    t1, t2 = st.tabs(["1. Metadata & Copyright", "2. Main Page (TOC)"])
    
    # --- TAB 1: METADATA ---
    with t1:
        c_talk, c_json = st.columns(2)
        
        # COLUMN 1: TALK PAGE
        with c_talk:
            st.subheader("Talk Page Text")
            talk_text = st.text_area("Clean OCR", 
                                     value=st.session_state.get("talk_text", ""), 
                                     height=500, key="talk_editor")
            
            if st.button(f"☁️ Import to Talk:{st.session_state['target_page']}", type="primary", use_container_width=True):
                # Placeholder for mediawiki upload
                # upload_to_wiki(f"Talk:{st.session_state['target_page']}", talk_text)
                st.success(f"Uploaded to Talk:{st.session_state['target_page']}")

        # COLUMN 2: WIKIBASE ITEM
        with c_json:
            st.subheader("Wikibase Data (JSON)")
            json_text = st.text_area("Metadata", 
                                     value=st.session_state.get("meta_json_str", "{}"), 
                                     height=500, key="meta_editor")
            
            if st.button("☁️ Import to Bahaidata", type="primary", use_container_width=True):
                try:
                    data = json.loads(json_text)
                    with st.spinner("Creating Item..."):
                        new_qid = import_book_to_wikibase(data)
                        st.session_state["parent_qid"] = new_qid # SAVE QID FOR TAB 2
                        st.success(f"Created Item: {new_qid}")
                        st.toast(f"Parent QID set to {new_qid}")
                except Exception as e:
                    st.error(f"Error: {e}")

    # --- TAB 2: CONTENT & CHAPTERS ---
    with t2:
        c_toc_json, c_toc_wiki = st.columns(2)
        
        # COLUMN 1: CHAPTER ITEMS
        with c_toc_json:
            st.subheader("Chapter Data (JSON)")
            toc_json_text = st.text_area("Chapters", 
                                         value=st.session_state.get("toc_json_str", "[]"), 
                                         height=400, key="toc_json_editor")
            
            # Parent QID Input (Auto-filled)
            parent_qid = st.text_input("Parent Book QID (P361)", 
                                       value=st.session_state.get("parent_qid", ""),
                                       help="If the book item exists, enter QID here.")
            
            if st.button("☁️ Import Chapters to Bahaidata", type="primary", use_container_width=True):
                if not parent_qid:
                    st.error("Please provide a Parent Book QID first.")
                else:
                    st.info("Need 'import_chapters_script.py' logic here. (Placeholder)")
                    # Logic: Loop through JSON, create items, link P361 to parent_qid

        # COLUMN 2: MAIN PAGE SOURCE
        with c_toc_wiki:
            st.subheader("Main Page Source")
            full_page_text = st.text_area("Wikitext", 
                                          value=default_full_page, 
                                          height=470, key="full_page_editor")
            
            if st.button(f"☁️ Import to {st.session_state['target_page']}", type="primary", use_container_width=True):
                # Placeholder for mediawiki upload
                # upload_to_wiki(st.session_state['target_page'], full_page_text)
                st.success(f"Uploaded to {st.session_state['target_page']}")

    st.divider()
    if st.button("⬅️ Back to Range Selection"):
        st.session_state.pipeline_stage = "setup"
        st.rerun()
