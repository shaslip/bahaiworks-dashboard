import streamlit as st
import os
import json
import pandas as pd
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.gemini_processor import extract_metadata_from_pdf, extract_toc_from_pdf, json_to_wikitext
from src.wikibase_importer import import_book_to_wikibase
from src.mediawiki_uploader import upload_to_bahaiworks
from src.chapter_importer import import_chapters_to_wikibase
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
    
    if "target_page" not in st.session_state:
        st.session_state["target_page"] = os.path.splitext(filename)[0].replace("_", " ")
    
    txt_path = file_path.replace(".pdf", ".txt")
    has_txt = os.path.exists(txt_path)

if "pipeline_stage" not in st.session_state:
    st.session_state.pipeline_stage = "setup" 

# --- UI Header ---
c1, c2 = st.columns([3, 1])
with c1:
    st.title(f"📖 {filename}")
    target_page = st.text_input("🎯 Bahai.works Page Title", 
                                value=st.session_state["target_page"],
                                key="global_target_input")
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
        with st.spinner("🤖 Gemini is extracting..."):
            if cr_pages:
                res = extract_metadata_from_pdf(file_path, cr_pages)
                if "error" not in res:
                    st.session_state["meta_result"] = res
                    st.session_state["talk_text"] = res.get("copyright_text", "")
                    st.session_state["meta_json_str"] = json.dumps(res.get("data", {}), indent=4)

            if toc_pages:
                res = extract_toc_from_pdf(file_path, toc_pages)
                if "error" not in res:
                    st.session_state["toc_json_list"] = res.get("toc_json", [])
            
            st.session_state.pipeline_stage = "proof"
            st.rerun()

# --- STAGE 2: PROOFREAD & IMPORT ---
elif st.session_state.pipeline_stage == "proof":
    
    t1, t2 = st.tabs(["1. Metadata (Book Item)", "2. Content (Chapters & Pages)"])
    
    # --- TAB 1: METADATA ---
    with t1:
        c_talk, c_json = st.columns(2)
        with c_talk:
            st.subheader("Talk Page")
            talk_text = st.text_area("Clean OCR", value=st.session_state.get("talk_text", ""), height=500, key="talk_edit")
            if st.button(f"☁️ Import to Talk:{target_page}", type="primary", width='stretch'):
                try:
                    upload_to_bahaiworks(f"Talk:{target_page}", talk_text, "Init OCR")
                    st.success("✅ Uploaded")
                except Exception as e: st.error(str(e))

        with c_json:
            st.subheader("Wikibase Item")
            json_text = st.text_area("JSON", value=st.session_state.get("meta_json_str", "{}"), height=500, key="meta_edit")
            
            c_btn1, c_btn2 = st.columns(2)
            with c_btn1:
                if st.button("1. Create Item", type="primary", width='stretch'):
                    try:
                        qid = import_book_to_wikibase(json.loads(json_text))
                        st.session_state["parent_qid"] = qid
                        st.success(f"Created: {qid}")
                    except Exception as e: st.error(str(e))
            with c_btn2:
                if "parent_qid" in st.session_state:
                    if st.button("2. Link Page", width='stretch'):
                        ok, msg = set_sitelink(st.session_state["parent_qid"], target_page)
                        if ok: st.success("Linked")
                        else: st.error(msg)

    # --- TAB 2: CONTENT (The New Workflow) ---
    with t2:
        # Prepare Data for Editor
        if "toc_json_list" not in st.session_state:
            st.session_state["toc_json_list"] = []
        
        # Flatten authors list to string for editing
        raw_data = []
        for item in st.session_state["toc_json_list"]:
            authors_str = ", ".join(item.get("author", []))
            raw_data.append({
                "Title": item.get("title", ""),
                "Page Range": item.get("page_range", ""),
                "Authors": authors_str
            })
        
        df = pd.DataFrame(raw_data)

        # Layout: Data Editor (Left) | Actions (Right)
        c_editor, c_preview, c_actions = st.columns([2, 2, 1])
        
        # --- COLUMN 1: MASTER DATA ---
        with c_editor:
            st.subheader("1. Edit Chapter Data (Master)")
            st.caption("Fix titles and ranges here. This drives everything else.")
            edited_df = st.data_editor(df, num_rows="dynamic", width='stretch', height=600)
            
            # Reconstruct JSON from Editor
            updated_toc_list = []
            for index, row in edited_df.iterrows():
                # Split authors back into list
                auth_list = [a.strip() for a in row["Authors"].split(",") if a.strip()]
                updated_toc_list.append({
                    "title": row["Title"],
                    "page_range": row["Page Range"],
                    "author": auth_list
                })
            
            # Sync back to session state so it persists
            st.session_state["toc_json_list"] = updated_toc_list
            
            # Auto-Compute Wikitext from the Master Data
            computed_toc_wikitext = json_to_wikitext(updated_toc_list)

        # --- COLUMN 2: PREVIEW ---
        with c_preview:
            st.subheader("2. Page Preview (Computed)")
            st.caption("Read-only view of the TOC. Edit the header if needed.")
            
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
            # We combine the static header with the dynamic TOC
            full_wikitext = header_template + computed_toc_wikitext
            st.code(full_wikitext, language="mediawiki")

        # --- COLUMN 3: ACTIONS ---
        with c_actions:
            st.subheader("3. Execute")
            
            target_title = st.session_state['target_page']
            parent_qid = st.text_input("Parent QID", value=st.session_state.get("parent_qid", ""))
            
            st.markdown("---")
            st.write("**A. Bahai.works**")

            # ACTION 1: Upload Main Page
            if st.button(f"1. Create {target_title}", type="primary", width='stretch'):
                try:
                    upload_to_bahaiworks(target_title, full_wikitext, "Setup")
                    st.success(f"✅ Created {target_title}")
                except Exception as e: st.error(str(e))

            # ACTION 2: Create Subpages
            if st.button("2. Create Chapter Placeholders", width='stretch'):
                try:
                    access_group = target_title.replace(" ", "")
                    content = f"<accesscontrol>Access:{access_group}</accesscontrol>{{{{Publicationinfo}}}}"
                    
                    progress = st.progress(0)
                    
                    for i, item in enumerate(updated_toc_list):
                        full_title = f"{target_title}/{item['title']}"
                        # Only create the page (Linking happens in Step 3 now)
                        upload_to_bahaiworks(full_title, content, "Chapter placeholder")
                        progress.progress((i+1)/len(updated_toc_list))
                    
                    st.success("✅ Placeholders Created")
                except Exception as e: st.error(str(e))

            st.markdown("---")
            st.write("**B. Bahaidata**")
            
            # ACTION 3a: Simple Link (Main Book Only)
            if st.button("3a. Link Book Item Only", width='stretch', help="Links the Parent QID to the Main Bahai.works page. Use this if you are NOT creating chapter items."):
                if not parent_qid:
                    st.error("Need Parent QID")
                else:
                    try:
                        success, msg = set_sitelink(parent_qid, target_title)
                        if success: st.success(f"✅ Linked {parent_qid} -> {target_title}")
                        else: st.error(msg)
                    except Exception as e: st.error(str(e))

            # ACTION 3b: Import & Link (Scholarly Pipeline)
            if st.button("3b. Import & Link Chapter Items", type="primary", width='stretch', help="Creates items for every chapter and links them to the subpages."):
                if not parent_qid:
                    st.error("Need Parent QID")
                else:
                    try:
                        with st.spinner("Creating Items & Linking..."):
                            # 1. Create Items
                            logs, created_map = import_chapters_to_wikibase(parent_qid, updated_toc_list)
                            st.session_state["chapter_qid_map"] = created_map
                            
                            # 2. Link Items immediately
                            link_logs = []
                            for item in created_map:
                                qid = item['qid']
                                title = item['title']
                                full_page_url = f"{target_title}/{title}"
                                
                                success, msg = set_sitelink(qid, full_page_url)
                                if success: link_logs.append(f"🔗 Linked {qid} -> {title}")
                                else: link_logs.append(f"❌ Link Fail {qid}: {msg}")
                            
                            st.success(f"✅ Processed {len(created_map)} Chapters")
                            with st.expander("Activity Log"):
                                st.write(logs)
                                st.write(link_logs)
                                
                    except Exception as e: st.error(str(e))
            
            st.markdown("---")
            
            # FINAL: Move to Splitter
            if st.button("🏁 Proceed to Splitter", width='stretch'):
                st.session_state["toc_map"] = updated_toc_list
                st.session_state.pipeline_stage = "split"
                st.rerun()
