import streamlit as st
import os
import re
import json
import hashlib
import pandas as pd
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.gemini_processor import extract_metadata_from_pdf, extract_toc_from_pdf
from src.wikibase_importer import import_book_to_wikibase
from src.mediawiki_uploader import upload_to_bahaiworks
from src.sitelink_manager import set_sitelink
from src.text_processing import parse_text_file, find_best_match_for_title
from src.evaluator import translate_summary

st.set_page_config(layout="wide", page_title="Publication Pipeline")

# --- Helper: Header Generator ---
def generate_header(title, author, year, language, is_copyright, filename):
    """Generates the appropriate MediaWiki header based on Language/Copyright."""
    
    if language == "German":
        # German Template
        header = f"""{{{{header
 | title      = {title}
 | author     = {author}
 | translator = 
 | section    = 
 | previous   = 
 | next       = 
 | year       = {year}
 | notes      = {{{{home |link= | pdf=[{{{{filepath:{filename}}}}} PDF] }}}}
}}}}"""
    else:
        # English Template
        header = f"""{{{{header
 | title      = {title}
 | author     = {author}
 | translator = 
 | compiler   = 
 | section    = 
 | previous   = 
 | next       = 
 | publisher  = 
 | year       = {year}
 | notes      = 
 | categories = All publications/Books
 | portal     = 
}}}}"""

    return header

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
    
    # Default Target Page Name
    if "target_page" not in st.session_state:
        st.session_state["target_page"] = os.path.splitext(filename)[0].replace("_", " ")
    
    txt_path = file_path.replace(".pdf", ".txt")
    has_txt = os.path.exists(txt_path)

if "pipeline_stage" not in st.session_state:
    st.session_state.pipeline_stage = "setup"

# --- UI Header ---
st.title(f"📖 {filename}")

# Determine Default Language Index
lang_options = ["English", "German"]
default_lang_index = 0
if record.language and record.language.lower() in ["german", "de", "deutsch"]:
    default_lang_index = 1

# Global Config Row
g1, g2, g3, g4 = st.columns(4)
with g1:
    st.session_state["target_page"] = st.text_input("🎯 Page Title", value=st.session_state["target_page"])
with g2:
    # CHANGED: Radio -> Selectbox with Smart Default
    pub_language = st.selectbox("Language", lang_options, index=default_lang_index, key="cfg_lang")
with g3:
    # CHANGED: Radio -> Selectbox, Default to Unstructured
    # Note: If you want Unstructured as default, it must be 0th index
    type_options = ["Unstructured", "Book", "Periodical"]
    pub_type = st.selectbox("Type", type_options, index=0, key="cfg_type")
with g4:
    st.write("Permissions")
    is_copyright = st.checkbox("Copyright Protected?", value=False, key="cfg_copy")

if not has_txt:
    st.error(f"❌ Critical: No OCR text file found at {txt_path}.")
    st.stop()

st.divider()

# ==============================================================================
# STAGE 1: SETUP & EXTRACTION
# ==============================================================================
if st.session_state.pipeline_stage == "setup":
    
    # Only show extraction tools for Books
    if pub_type == "Book":
        st.info("Configure extraction or enter data manually.")
        
        c_cr, c_toc = st.columns(2)
        with c_cr:
            st.subheader("©️ Copyright Pages")
            cr_pages = st.text_input("Range (e.g. 1-2)", key="cr_input")
        with c_toc:
            st.subheader("📑 TOC Pages")
            toc_pages = st.text_input("Range (e.g. 5-8)", key="toc_input")

        st.markdown("---")

        b_gemini, b_manual = st.columns([1, 1])
        
        # A. Send to Gemini
        with b_gemini:
            if st.button("🚀 Send to Gemini", type="primary", use_container_width=True):
                with st.spinner("🤖 Gemini is extracting..."):
                    
                    # 1. Extract Metadata (Copyright)
                    if cr_pages:
                        res = extract_metadata_from_pdf(file_path, cr_pages)
                        if "error" not in res:
                            st.session_state["meta_result"] = res
                            st.session_state["talk_text"] = res.get("copyright_text", "")
                            st.session_state["meta_json_str"] = json.dumps(res.get("data", {}), indent=4)
                        else:
                            st.error(f"Metadata Extraction Failed: {res['error']}")

                    # 2. Extract TOC
                    if toc_pages:
                        res = extract_toc_from_pdf(file_path, toc_pages)
                        if "error" not in res:
                            st.session_state["toc_json_list"] = res.get("toc_json", [])
                            st.session_state.pipeline_stage = "proof"
                            st.rerun()
                        else:
                            st.error(f"TOC Extraction Failed: {res['error']}")
        
        # B. Manual Entry (Bypass)
        with b_manual:
            if st.button("✍️ Skip Extraction (Manual Entry)", use_container_width=True):
                # Initialize empty state
                st.session_state["talk_text"] = ""
                st.session_state["meta_json_str"] = "{\n    \"title\": \"\",\n    \"author\": [],\n    \"year\": \"\"\n}"
                st.session_state["toc_json_list"] = []
                st.session_state.pipeline_stage = "proof"
                st.rerun()

    else:
        # Non-Book Flow (Immediate Jump)
        st.info(f"Simple workflow selected for **{pub_type}**. Proceeding to editor.")
        if st.button("Proceed", type="primary"):
            st.session_state.pipeline_stage = "proof"
            st.rerun()

# ==============================================================================
# STAGE 2: PROOFREAD & IMPORT
# ==============================================================================
elif st.session_state.pipeline_stage == "proof":
    
    # Initialize JSON Version Counter for Syncing
    if "toc_version" not in st.session_state:
        st.session_state["toc_version"] = 0

    # NEW: Navigation to return to Setup
    if st.button("⬅️ Back to Setup", key="back_to_setup"):
        st.session_state.pipeline_stage = "setup"
        st.rerun()

    # --------------------------
    # BRANCH: BOOK WORKFLOW
    # --------------------------
    if pub_type == "Book":
        t1, t2 = st.tabs(["1. Metadata (Book Item)", "2. Content (Chapters & Pages)"])
        
        # --- TAB 1: METADATA ---
        with t1:
            c_talk, c_item, c_toc = st.columns(3)
            
            # COLUMN 1: TALK PAGE
            with c_talk:
                st.subheader(f"Talk:{st.session_state['target_page']}")
                talk_text = st.text_area("Copyright / Talk Page Text", value=st.session_state.get("talk_text", ""), height=500, key="talk_edit")
                
                if st.button(f"☁️ Upload Talk Page", type="primary", width='stretch'):
                    try:
                        upload_to_bahaiworks(
                            f"Talk:{st.session_state['target_page']}", 
                            talk_text, 
                            "Init OCR", 
                            check_exists=True
                        )
                        st.success("✅ Uploaded")
                    except Exception as e: st.error(str(e))

            # COLUMN 2: WIKIBASE ITEM
            with c_item:
                st.subheader("Wikibase Item")
                json_text = st.text_area("Item JSON", value=st.session_state.get("meta_json_str", "{}"), height=500, key="meta_edit")
                
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
                            ok, msg = set_sitelink(st.session_state["parent_qid"], st.session_state['target_page'])
                            if ok: st.success("Linked")
                            else: st.error(msg)

            # COLUMN 3: TOC JSON (Source)
            with c_toc:
                st.subheader("TOC JSON (Source)")
                current_toc = st.session_state.get("toc_json_list", [])
                
                # Format JSON with safe characters
                toc_str = json.dumps(current_toc, indent=2, ensure_ascii=False)
                
                # Key changes whenever 'toc_version' increments (from Tab 2 edits)
                toc_edit_text = st.text_area(
                    "Structure Data", 
                    value=toc_str, 
                    height=500, 
                    key=f"toc_edit_{st.session_state.toc_version}"
                )
                
                if st.button("💾 Update Content Tab", type="secondary", width="stretch"):
                    try:
                        st.session_state["toc_json_list"] = json.loads(toc_edit_text)
                        
                        # Increment version so this tab stays in sync with itself
                        st.session_state["toc_version"] += 1
                        
                        # FORCE REFRESH: Delete cached DF so Tab 2 rebuilds
                        if "chapter_df" in st.session_state:
                            del st.session_state["chapter_df"]
                            
                        st.success("Updated! Check Tab 2.")
                        st.rerun() 
                    except Exception as e:
                        st.error(f"Invalid JSON: {e}")

        # --- TAB 2: CONTENT ---
        with t2:
            # 1. Initialize State Variables
            if "toc_json_list" not in st.session_state:
                st.session_state["toc_json_list"] = []

            # 2. Build the Static Initial DataFrame (ONLY ONCE)
            if "chapter_df" not in st.session_state:
                raw_data = []
                toc_source = st.session_state.get("toc_json_list", [])
                
                for i, item in enumerate(toc_source):
                    original_title = item.get("title", "")
                    
                    # --- SAFETY FIX: Handle None/Null Levels ---
                    raw_level = item.get("level", 1)
                    try:
                        level = 1 if (raw_level is None or raw_level == "") else int(raw_level)
                    except (ValueError, TypeError):
                        level = 1
                    # -------------------------------------------
                    
                    clean_title = original_title
                    prefix = item.get("prefix", "")
                    
                    if not prefix:
                        match = re.match(r"^(\d+(?:[./]\s*|\s+))", original_title)
                        if match:
                            prefix = match.group(1) 
                            clean_title = original_title[len(prefix):].strip()
                    
                    p_name = item.get("page_name", clean_title)
                    d_title = item.get("display_title", clean_title)
                    if p_name and p_name.isupper(): p_name = p_name.title()

                    # --- RESTORED LOGIC: Smart Container Detection ---
                    # If Level 1, look ahead for authored children.
                    if level == 1:
                        has_authored_children = False
                        for j in range(i + 1, len(toc_source)):
                            next_item = toc_source[j]
                            
                            # Safety check for next item level too
                            nl_raw = next_item.get("level", 1)
                            try: next_level = int(nl_raw) if nl_raw else 1
                            except: next_level = 1
                            
                            if next_level == 1: break # Hit next sibling
                            
                            # If child has authors, parent is a Container
                            child_authors = next_item.get("author", [])
                            if child_authors and len(child_authors) > 0:
                                has_authored_children = True
                                break
                        
                        # Clear Page Name -> defaults to Plain Text Header
                        if has_authored_children:
                            p_name = ""
                    # ------------------------------------------------

                    authors_str = ", ".join(item.get("author", []))
                    
                    raw_data.append({
                        "Level": level,
                        "Prefix": prefix,
                        "Page Name (URL)": p_name,
                        "Display Title": d_title,
                        "Page Range": item.get("page_range", ""),
                        "Authors": authors_str
                    })
                st.session_state["chapter_df"] = pd.DataFrame(raw_data)

            # 3. Layout
            c_editor, c_preview, c_actions = st.columns([2, 2, 1])
            
            # --- COLUMN 1: DATA EDITOR ---
            with c_editor:
                st.subheader("1. Edit Chapter Data")
                
                column_config = {
                    "Level": st.column_config.NumberColumn("Lvl", min_value=1, max_value=3, width="small"),
                    "Prefix": st.column_config.TextColumn("Prefix", width="small"),
                    "Page Name (URL)": st.column_config.TextColumn("Page Name (URL)", width="medium"),
                    "Display Title": st.column_config.TextColumn("Display Title", width="medium"),
                }
                
                # We edit the SESSION STATE dataframe directly.
                edited_df = st.data_editor(
                    st.session_state["chapter_df"], 
                    num_rows="dynamic", 
                    width='stretch',
                    height=600,
                    column_config=column_config,
                    key="chapter_editor"
                )
                
                # 4. Process Updates (DataFrame -> JSON/Wikitext)
                updated_toc_list = []
                computed_toc_wikitext = ""
                current_section_is_container = False 
                
                for index, row in edited_df.iterrows():
                    # Extract & Clean
                    raw_authors = str(row["Authors"]) if row["Authors"] else ""
                    auth_list = [a.strip() for a in raw_authors.split(",") if a.strip()]
                    
                    p_name = row["Page Name (URL)"]
                    d_title = row["Display Title"]
                    prefix = row["Prefix"]
                    
                    # Safe Cast for Level (Crash Fix)
                    try:
                        val = row["Level"]
                        level = 1 if (pd.isna(val) or val == "") else int(val)
                    except: level = 1

                    if prefix is None: prefix = ""
                    
                    # Reconstruct JSON Object
                    updated_toc_list.append({
                        "title": d_title,
                        "page_name": p_name,
                        "display_title": d_title,
                        "prefix": prefix,
                        "level": level,
                        "page_range": row["Page Range"],
                        "author": auth_list
                    })
                    
                    # Generate Wikitext
                    indent = ":" * level
                    
                    if level == 1:
                        # Logic A: Level 1 (Chapters / Sections)
                        if not p_name or not p_name.strip():
                            current_section_is_container = True
                            computed_toc_wikitext += f"\n:{prefix}{d_title}" 
                        else:
                            current_section_is_container = False
                            computed_toc_wikitext += f"\n:{prefix}[[/{p_name}|{d_title}]]" 
                    else:
                        # Logic B: Sub-sections
                        should_link = (len(auth_list) > 0) or current_section_is_container
                        
                        if should_link:
                            computed_toc_wikitext += f"\n{indent}{prefix}[[/{p_name}|{d_title}]]"
                            if auth_list:
                                authors_str = ", ".join(auth_list)
                                computed_toc_wikitext += f"\n{indent}: ''{authors_str}''"
                        else:
                            computed_toc_wikitext += f"\n{indent}{prefix}{d_title}"
                
                # Sync back to JSON (Source of Truth)
                # We check if data actually changed to avoid unnecessary re-renders
                if st.session_state["toc_json_list"] != updated_toc_list:
                    st.session_state["toc_json_list"] = updated_toc_list
                    st.session_state["toc_version"] += 1

            # --- COLUMN 2: PREVIEW ---
            with c_preview:
                st.subheader("2. Page Preview")
                
                header_text = generate_header(
                    title=st.session_state["target_page"],
                    author="[Author]", 
                    year="[Year]",
                    language=pub_language,
                    is_copyright=is_copyright,
                    filename=filename
                )
                
                if is_copyright:
                    header_text = "{{restricted use|where=|until=}}\n" + header_text

                book_template = """
{{book
 | color = 656258
 | image = 
 | downloads = 
 | translations = 
 | pages = 
 | links = 
}}"""
                full_wikitext = header_text + book_template + "\n\n===Contents===\n" + computed_toc_wikitext
                st.code(full_wikitext, language="mediawiki")

            # --- COLUMN 3: ACTIONS ---
            with c_actions:
                st.subheader("3. Execute")
                target_title = st.session_state['target_page']
                parent_qid = st.text_input("Parent QID", value=st.session_state.get("parent_qid", ""))
                
                st.markdown("---")
                
                if st.button(f"1. Create {target_title}", type="primary", width='stretch'):
                    try:
                        upload_to_bahaiworks(target_title, full_wikitext, "Setup (Book Pipeline)", check_exists=True)
                        st.success(f"✅ Created {target_title}")
                    except Exception as e: st.error(str(e))
                
                st.markdown("---")
                
                if st.button("Connect Book item", width='stretch'):
                    if not parent_qid: st.error("Need Parent QID")
                    else:
                        try:
                            success, msg = set_sitelink(parent_qid, target_title)
                            if success: st.success("✅ Linked")
                            else: st.error(msg)
                        except Exception as e: st.error(str(e))
                
                st.markdown("---")
                if st.button("🏁 Proceed to Splitter", width='stretch'):
                    st.session_state["toc_map"] = updated_toc_list
                    st.session_state.pipeline_stage = "split"
                    st.rerun()

    # --------------------------
    # BRANCH: SIMPLE WORKFLOW (Periodical / Unstructured)
    # --------------------------
    else:
        st.subheader(f"📝 {pub_type} Details")
        
        c_sim1, c_sim2 = st.columns(2)
        with c_sim1:
            sim_title = st.text_input("Title", value=st.session_state["target_page"])
            sim_author = st.text_input("Author")
            sim_year = st.text_input("Year")
            
            # --- RESTORED & DYNAMIC TRANSLATION LOGIC ---
            st.write("**Summary / Abstract**")
            summary_key = f"summary_{doc_id}"
            
            # Load initial summary from DB if not in state
            if summary_key not in st.session_state:
                st.session_state[summary_key] = record.summary or ""

            # Dynamic Translation Button
            # Only show if target is NOT English
            if pub_language != "English":
                if st.button(f"🤖 Translate to {pub_language}"):
                    with st.spinner(f"Translating to {pub_language}..."):
                        current_text = st.session_state.get(summary_key, "")
                        
                        # Note: Ensure src/evaluator.py accepts target_language
                        translated_text = translate_summary(current_text, target_language=pub_language)
                        
                        if translated_text:
                            st.session_state[summary_key] = translated_text
                            st.rerun()

            # The Text Area
            sim_summary = st.text_area("Content", key=summary_key, height=150)
            
        with c_sim2:
            st.info("Preview")
            
            # Generate Header
            header_text = generate_header(
                title=sim_title,
                author=sim_author,
                year=sim_year,
                language=pub_language,
                is_copyright=is_copyright,
                filename=filename
            )
            
            # Prepend Restricted Use if needed
            if is_copyright:
                header_text = "{{restricted use|where=|until=}}\n" + header_text
            
            wiki_body = ""
            if pub_type == "Periodical":
                wiki_body = f"<pdf>File:{filename}</pdf>"
            else:
                # Unstructured / Booklet
                summary_block = f"{{{{ai|{sim_summary}}}}}" if sim_summary else ""
                
                if pub_language == "German":
                     wiki_body = f"{summary_block}\n\n== Zugang ==\n* [{{{{filepath:{filename}}}}} PDF]\n* Für den Volltext siehe [[/Text]]."
                else:
                     wiki_body = f"{summary_block}\n\n* [{{{{filepath:{filename}}}}} PDF]"

            final_wikitext = f"{header_text}\n\n{wiki_body}"
            st.code(final_wikitext, language="mediawiki")
            
            if st.button("🚀 Upload & Complete", type="primary"):
                try:
                    upload_to_bahaiworks(sim_title, final_wikitext, f"Init {pub_type}")
                    st.success("Page Created!")
                    
                    # Mark DB as completed
                    with Session(engine) as session:
                        rec = session.get(Document, doc_id)
                        rec.status = "COMPLETED"
                        session.commit()
                    
                    st.success("Document marked as COMPLETED.")
                    if st.button("Return to Dashboard"): go_back()
                    
                except Exception as e:
                    st.error(str(e))

# ==============================================================================
# STAGE 3: SPLITTER (BOOKS ONLY)
# ==============================================================================
elif st.session_state.pipeline_stage == "split":
    st.header("Step 3: Verify & Split")
    
    # 1. Load & Parse Text (Once)
    if "page_map" not in st.session_state:
        with st.spinner("Indexing text file..."):
            p_map, p_order = parse_text_file(txt_path)
            st.session_state["page_map"] = p_map
            st.session_state["page_order"] = p_order
    
    page_map = st.session_state["page_map"]
    page_order = st.session_state["page_order"]
    
    # 2. Initialize Indices (Once)
    full_toc = st.session_state.get("toc_map", [])
    toc_list = [
        item for item in full_toc 
        if item.get('page_name') and str(item.get('page_name')).strip() != ""
    ]
    
    if "splitter_indices" not in st.session_state:
        indices = {}
        for i, item in enumerate(toc_list):
            title = item['title']
            raw_range = item.get('page_range', "")
            
            # Default to index 0
            idx = 0
            
            # Try to guess label from TOC
            guess = "1"
            if "-" in str(raw_range): 
                guess = str(raw_range).split("-")[0].strip()
            elif raw_range: 
                guess = str(raw_range).strip()
            
            # If not digit, try fuzzy match
            if not guess.isdigit():
                found = find_best_match_for_title(title, page_map, page_order)
                if found: guess = found
            
            try:
                idx = page_order.index(guess)
            except ValueError:
                idx = 0
                
            indices[i] = idx
        st.session_state["splitter_indices"] = indices

    # 3. Helper to adjust index (Previous/Next)
    def adjust_index(i, direction):
        curr = st.session_state["splitter_indices"][i]
        new = curr + direction
        if 0 <= new < len(page_order):
            st.session_state["splitter_indices"][i] = new

    # 4. Render Grid
    for i, item in enumerate(toc_list):
        current_idx = st.session_state["splitter_indices"][i]
        current_label = page_order[current_idx]
        
        c_title, c_nav, c_preview = st.columns([2, 1, 4])
        
        with c_title:
            st.subheader(item['title'])
            st.caption(f"TOC Range: {item.get('page_range', 'N/A')}")
        
        with c_nav:
            st.write(f"**Tag: `{{{{page|{current_label}}}}}`**")
            
            c_minus, c_plus = st.columns(2)
            with c_minus:
                if st.button("◀", key=f"prev_{i}", width='stretch'): 
                    adjust_index(i, -1)
                    st.rerun()
            with c_plus:
                if st.button("▶", key=f"next_{i}", width='stretch'):
                    adjust_index(i, 1)
                    st.rerun()

        with c_preview:
            preview_text = page_map.get(current_label, "Error: Content missing")
            st.text_area("Preview", value=preview_text[:400]+"...", height=120, key=f"pview_{i}_{current_idx}", disabled=True)
            
        st.divider()

    # 5. Actions
    c_back, c_run = st.columns([1, 4])
    with c_back:
        if st.button("⬅️ Back"):
            st.session_state.pipeline_stage = "proof"
            st.rerun()
            
    with c_run:
        if st.button("✂️ Split & Upload", type="primary"):
            target_base = st.session_state["target_page"]
            
            # Access Header for Chapters (Content Pages)
            # <accesscontrol> goes here ONLY if copyright is True
            if is_copyright:
                 # Clean group name for access control
                 access_group = target_base.replace(" ", "")
                 header_content = f"<accesscontrol>Access:{access_group}</accesscontrol>{{{{Publicationinfo}}}}\n"
            else:
                 # No access control, just the nav template
                 header_content = f"{{{{Publicationinfo}}}}\n"

            progress_bar = st.progress(0)
            status_box = st.empty()
            
            try:
                final_split_data = []
                for i, item in enumerate(toc_list):
                    # FILTER: Only split Level 1 items.
                    if item.get('level', 1) == 1:
                        final_split_data.append({
                            "title": item['title'],
                            "page_name": item.get('page_name', item['title']), 
                            "start_idx": st.session_state["splitter_indices"][i]
                        })

                if not final_split_data:
                    st.error("No Level 1 chapters found to split!")
                    st.stop()

                for i, chapter in enumerate(final_split_data):
                    ch_title = chapter['title']
                    ch_page_name = chapter['page_name']
                    start_idx = chapter['start_idx']
                    
                    if i + 1 < len(final_split_data):
                        end_idx = final_split_data[i+1]['start_idx']
                    else:
                        end_idx = len(page_order)
                    
                    status_box.write(f"Processing {ch_title}...")
                    
                    raw_text = ""
                    for p_idx in range(start_idx, end_idx):
                        p_label = page_order[p_idx]
                        raw_text += page_map[p_label]
                    
                    full_content = header_content + raw_text
                    
                    full_title = f"{target_base}/{ch_page_name}"
                    upload_to_bahaiworks(full_title, full_content, "Splitter Upload")
                    
                    progress_bar.progress((i + 1) / len(final_split_data))
                    
                status_box.success("✅ All chapters split and uploaded!")
                st.balloons()
                st.session_state["split_completed"] = True
                
            except Exception as e:
                st.error(f"Failed: {e}")

        if st.session_state.get("split_completed"):
            st.divider()
            if st.button("📝 Review & Create Chapter Items", type="primary", width='stretch'):
                # Pass Data to Chapter Manager
                chapter_payload = []
                full_toc = st.session_state.get("toc_map", [])
                
                for item in full_toc:
                    if item.get("page_name") and str(item.get("page_name")).strip() != "":
                        chapter_payload.append(item)
                
                st.session_state["chapter_review_data"] = chapter_payload
                st.session_state["chapter_parent_qid"] = st.session_state.get("parent_qid", "")
                st.session_state["chapter_target_base"] = st.session_state.get("target_page", "")
                
                st.switch_page("pages/05_chapter_items.py")
