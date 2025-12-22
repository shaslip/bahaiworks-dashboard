import streamlit as st
import os
import json
from src.mediawiki_uploader import upload_to_bahaiworks
from src.chapter_importer import import_chapters_to_wikibase
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.gemini_processor import extract_metadata_from_pdf, extract_toc_from_pdf
from src.wikibase_importer import import_book_to_wikibase
from src.sitelink_manager import set_sitelink

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

    t1, t2 = st.tabs(["1. Metadata & Copyright", "2. Main Page (TOC)"])
    
    # --- TAB 1: METADATA ---
    with t1:
        c_talk, c_json = st.columns(2)
        
        # COLUMN 1: TALK PAGE
        with c_talk:
            st.subheader("Talk Page")
            talk_text = st.text_area("Clean OCR", value=st.session_state.get("talk_text", ""), height=500, key="talk_editor")
            
            talk_title = f"Talk:{st.session_state['target_page']}"
            if st.button(f"☁️ Import to {talk_title}", type="primary", use_container_width=True):
                with st.spinner("Uploading..."):
                    try:
                        upload_to_bahaiworks(talk_title, talk_text, summary="Initial OCR upload")
                        st.success(f"✅ Uploaded to {talk_title}")
                    except Exception as e:
                        st.error(f"Upload Error: {e}")

        # COLUMN 2: WIKIBASE ITEM
        with c_json:
            st.subheader("Wikibase Data")
            json_text = st.text_area("Metadata", value=st.session_state.get("meta_json_str", "{}"), height=500, key="meta_editor")
            
            # A. Create Item
            if st.button("1. ☁️ Create Book Item", type="primary", use_container_width=True):
                try:
                    data = json.loads(json_text)
                    with st.spinner("Creating Item..."):
                        new_qid = import_book_to_wikibase(data)
                        st.session_state["parent_qid"] = new_qid 
                        st.success(f"✅ Created Item: {new_qid}")
                except Exception as e:
                    st.error(f"Error: {e}")

            # B. Link Item (Only appears if we have a QID)
            if "parent_qid" in st.session_state:
                target_title = st.session_state['target_page']
                if st.button(f"2. 🔗 Link {st.session_state['parent_qid']} to '{target_title}'", use_container_width=True):
                    success, msg = set_sitelink(st.session_state["parent_qid"], target_title)
                    if success: st.success(msg)
                    else: st.error(msg)

    # --- TAB 2: CONTENT & CHAPTERS ---
    with t2:
        c_toc_json, c_toc_wiki = st.columns(2)
        
        # COLUMN 1: CHAPTER ITEMS
        with c_toc_json:
            st.subheader("Chapter Data")
            toc_json_text = st.text_area("Chapters", value=st.session_state.get("toc_json_str", "[]"), height=400, key="toc_json_editor")
            
            parent_qid = st.text_input("Parent Book QID (P361)", value=st.session_state.get("parent_qid", ""))
            
            # A. Import Chapters
            if st.button("1. ☁️ Import Chapters to Bahaidata", type="primary", use_container_width=True):
                if not parent_qid:
                    st.error("Parent QID required.")
                else:
                    try:
                        chapters_data = json.loads(toc_json_text)
                        with st.spinner(f"Processing {len(chapters_data)} chapters..."):
                            # Update: Now returns a map of created items
                            logs, created_map = import_chapters_to_wikibase(parent_qid, chapters_data)
                            st.session_state["chapter_map"] = created_map
                            st.success(f"✅ Created {len(created_map)} Items")
                            with st.expander("Log"):
                                st.write(logs)
                    except Exception as e:
                        st.error(f"Failed: {e}")

            # B. Create Pages & Sitelinks (Only appears if we have the map)
            if "chapter_map" in st.session_state:
                st.info("Next: Create pages on Bahai.works and link them.")
                
                # ACCESS CONTROL TAG logic
                # Assuming the access group matches the book title (User can edit this logic if needed)
                access_group = st.session_state['target_page'].replace(" ", "")
                placeholder_content = f"<accesscontrol>Access:{access_group}</accesscontrol>{{{{Publicationinfo}}}}"
                
                if st.button("2. 📄 Create Pages & 🔗 Connect Links", type="primary", use_container_width=True):
                    chapter_map = st.session_state["chapter_map"]
                    base_title = st.session_state['target_page']
                    
                    progress_bar = st.progress(0)
                    log_container = st.container()
                    
                    for i, item in enumerate(chapter_map):
                        chapter_title = item['title']
                        chapter_qid = item['qid']
                        
                        # Construct subpage title: Book_Name/Chapter_Name
                        full_page_title = f"{base_title}/{chapter_title}"
                        
                        try:
                            # 1. Create Page
                            upload_to_bahaiworks(full_page_title, placeholder_content, summary="Chapter placeholder")
                            
                            # 2. Create Sitelink
                            set_sitelink(chapter_qid, full_page_title)
                            
                            log_container.write(f"✅ {full_page_title} <-> {chapter_qid}")
                        except Exception as e:
                            log_container.error(f"❌ Error on {chapter_title}: {e}")
                        
                        progress_bar.progress((i + 1) / len(chapter_map))
                    
                    st.success("Batch Operation Complete!")

        # COLUMN 2: MAIN PAGE SOURCE
        with c_toc_wiki:
            st.subheader("Main Page Source")
            full_page_text = st.text_area("Wikitext", value=default_full_page, height=470, key="full_page_editor")
            
            target_title = st.session_state['target_page']
            
            # Upload Main Page
            if st.button(f"☁️ Import to {target_title}", type="primary", use_container_width=True):
                with st.spinner("Uploading..."):
                    try:
                        upload_to_bahaiworks(target_title, full_page_text, summary="Initial setup")
                        st.success(f"✅ Uploaded to {target_title}")
                        
                        # Set stage for Splitting
                        st.session_state["toc_map"] = json.loads(toc_json_text)
                        st.session_state["final_toc_wikitext"] = full_page_text
                        st.session_state.pipeline_stage = "split" 
                        st.rerun()
                    except Exception as e:
                        st.error(f"Upload Error: {e}")

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
                        st.session_state["parent_qid"] = new_qid 
                        st.success(f"✅ Created Item: {new_qid}")
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
                    try:
                        chapters_data = json.loads(toc_json_text)
                        with st.spinner(f"Creating items for {len(chapters_data)} chapters..."):
                            logs = import_chapters_to_wikibase(parent_qid, chapters_data)
                            
                        st.success("✅ Chapters Processed!")
                        with st.expander("See Activity Log", expanded=True):
                            for log in logs:
                                st.write(f"- {log}")
                                
                    except json.JSONDecodeError:
                        st.error("Invalid JSON in Chapters box.")
                    except Exception as e:
                        st.error(f"Chapter Import Failed: {e}")

        # COLUMN 2: MAIN PAGE SOURCE
        with c_toc_wiki:
            st.subheader("Main Page Source")
            full_page_text = st.text_area("Wikitext", 
                                          value=default_full_page, 
                                          height=470, key="full_page_editor")
            
            target_title = st.session_state['target_page']
            if st.button(f"☁️ Import to {target_title}", type="primary", use_container_width=True):
                with st.spinner("Uploading to Bahai.works..."):
                    try:
                        upload_to_bahaiworks(target_title, full_page_text, summary="Initial book setup")
                        st.success(f"✅ Uploaded to {target_title}")
                        st.balloons()
                        
                        # Set stage for the next major step (Splitting)
                        st.session_state["toc_map"] = json.loads(toc_json_text) # Store map for splitter
                        st.session_state["final_toc_wikitext"] = full_page_text
                        st.session_state.pipeline_stage = "split" 
                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"Upload Error: {e}")

    st.divider()
    if st.button("⬅️ Back to Range Selection"):
        st.session_state.pipeline_stage = "setup"
        st.rerun()
