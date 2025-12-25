import streamlit as st
import os
import re
import json
import pandas as pd
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.gemini_processor import extract_metadata_from_pdf, extract_toc_from_pdf, json_to_wikitext
from src.wikibase_importer import import_book_to_wikibase
from src.mediawiki_uploader import upload_to_bahaiworks
from src.chapter_importer import import_chapters_to_wikibase
from src.sitelink_manager import set_sitelink
from src.text_processing import parse_text_file, find_best_match_for_title

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
                    # If we have raw text, show it for debugging
                    if "raw" in res:
                        with st.expander("See Raw Gemini Response (Debug)"):
                            st.text(res["raw"])

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
                    upload_to_bahaiworks(
                        f"Talk:{target_page}", 
                        talk_text, 
                        "Init OCR", 
                        check_exists=True
                    )
                    st.success("✅ Uploaded")
                
                except FileExistsError:
                    st.warning(f"⚠️ Talk:{target_page} already exists. OCR text was NOT uploaded.")
                except Exception as e: 
                    st.error(str(e))

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

    # --- TAB 2: CONTENT ---
    with t2:
        if "toc_json_list" not in st.session_state:
            st.session_state["toc_json_list"] = []
        
        # Prepare Data for Editor
        raw_data = []
        toc_source = st.session_state["toc_json_list"]
        
        for i, item in enumerate(toc_source):
            original_title = item.get("title", "")
            level = item.get("level", 1) 
            
            # --- Prefix Extraction ---
            prefix = item.get("prefix", "")
            clean_title = original_title

            if not prefix:
                match = re.match(r"^(\d+(?:[./]\s*|\s+))", original_title)
                if match:
                    prefix = match.group(1) 
                    clean_title = original_title[len(prefix):].strip()
            
            p_name = item.get("page_name", clean_title)
            d_title = item.get("display_title", clean_title)
            
            # --- AUTO-FIX: ALL CAPS to Title Case ---
            if p_name and p_name.isupper():
                p_name = p_name.title()
            
            # --- NEW LOGIC: Smart Container Detection ---
            # If this is Level 1, look ahead to see if it acts as a "Section Header" for authored papers.
            if level == 1:
                has_authored_children = False
                # Loop through subsequent items
                for j in range(i + 1, len(toc_source)):
                    next_item = toc_source[j]
                    next_level = next_item.get("level", 1)
                    
                    # Stop if we hit the next sibling (another Chapter or Section)
                    if next_level == 1: 
                        break
                    
                    # If we find a child (Level > 1) that has authors, this is a Scholarly Container
                    if next_level > 1:
                        child_authors = next_item.get("author", [])
                        if child_authors and len(child_authors) > 0:
                            has_authored_children = True
                            break
                
                # If identified as a Container, clear the Page Name so it defaults to UNLINKED
                if has_authored_children:
                    p_name = ""
            # --------------------------------------------

            authors_str = ", ".join(item.get("author", []))
            
            raw_data.append({
                "Level": level,
                "Prefix": prefix,
                "Page Name (URL)": p_name,
                "Display Title": d_title,
                "Page Range": item.get("page_range", ""),
                "Authors": authors_str
            })
        
        df = pd.DataFrame(raw_data)

        # Layout
        c_editor, c_preview, c_actions = st.columns([2, 2, 1])
        
        # --- COLUMN 1: MASTER DATA ---
        with c_editor:
            st.subheader("1. Edit Chapter Data")
            st.caption("Level 1 = Linked Chapter. Level 2 = Indented Subtopic (Not linked, Not split).")
            
            column_config = {
                "Level": st.column_config.NumberColumn("Lvl", min_value=1, max_value=3, width="small"),
                "Prefix": st.column_config.TextColumn("Prefix", width="small"),
                "Page Name (URL)": st.column_config.TextColumn("Page Name (URL)", width="medium"),
                "Display Title": st.column_config.TextColumn("Display Title", width="medium"),
            }
            
            edited_df = st.data_editor(
                df, 
                num_rows="dynamic", 
                width='stretch',
                height=600,
                column_config=column_config
            )
            
            # Reconstruct JSON
            updated_toc_list = []
            computed_toc_wikitext = ""
            
            # STATE VARIABLE: Track if the current Level 1 is a "Container" (Unlinked)
            # If True, all subsequent Level 2 items will be forced to Link.
            current_section_is_container = False 
            
            for index, row in edited_df.iterrows():
                # Data cleanup
                raw_authors = str(row["Authors"]) if row["Authors"] else ""
                auth_list = [a.strip() for a in raw_authors.split(",") if a.strip()]
                
                p_name = row["Page Name (URL)"]
                d_title = row["Display Title"]
                prefix = row["Prefix"]
                level = int(row["Level"])
                
                if prefix is None: prefix = ""
                
                updated_toc_list.append({
                    "title": d_title,
                    "page_name": p_name,
                    "display_title": d_title,
                    "prefix": prefix,
                    "level": level,
                    "page_range": row["Page Range"],
                    "author": auth_list
                })
                
                # --- WIKITEXT GENERATION LOGIC ---
                indent = ":" * level
                
                # Logic A: Level 1 (Chapters / Sections)
                if level == 1:
                    # If Page Name is empty (either manually or via auto-detect), 
                    # we treat this as a CONTAINER.
                    if not p_name or not p_name.strip():
                        current_section_is_container = True
                        computed_toc_wikitext += f"\n:{prefix}{d_title}" # Plain Text Header
                    else:
                        current_section_is_container = False
                        computed_toc_wikitext += f"\n:{prefix}[[/{p_name}|{d_title}]]" # Linked Header

                # Logic B: Sub-sections (Level 2+)
                else:
                    # Link IF: (It has an explicit author) OR (We are inside a Container)
                    should_link = (len(auth_list) > 0) or current_section_is_container
                    
                    if should_link:
                        # Render as Link
                        computed_toc_wikitext += f"\n{indent}{prefix}[[/{p_name}|{d_title}]]"
                        
                        # Only add the author line if an author actually exists
                        if auth_list:
                            authors_str = ", ".join(auth_list)
                            computed_toc_wikitext += f"\n{indent}: ''{authors_str}''"
                    else:
                        # Render as Plain Text (Standard Book behavior)
                        computed_toc_wikitext += f"\n{indent}{prefix}{d_title}"
            
            st.session_state["toc_json_list"] = updated_toc_list

        # --- COLUMN 2: PREVIEW ---
        with c_preview:
            st.subheader("2. Page Preview")
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
                    upload_to_bahaiworks(
                        target_title,
                        full_wikitext,
                        "Setup (Book Pipeline)",
                        check_exists=True
                    )
                    st.success(f"✅ Created {target_title}")
                except FileExistsError:
                     st.error(f"⚠️ Page '{target_title}' already exists. Setup aborted to prevent overwrite.")
                except Exception as e: 
                    st.error(str(e))

            st.markdown("---")
            st.write("**B. Bahaidata**")
            
            # ACTION 2: Connect Book Item
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

                if "splitter_indices" in st.session_state:
                    del st.session_state["splitter_indices"]

                if "split_completed" in st.session_state:
                    del st.session_state["split_completed"]

                st.session_state.pipeline_stage = "split"
                st.rerun()

# --- STAGE 3: SPLITTER ---
elif st.session_state.pipeline_stage == "split":
    st.header("Step 3: Verify & Split")
    st.info("Review the start of each chapter. Use + / - to find the correct start page.")

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
            
            # Try to guess label from TOC
            guess = "1"
            if "-" in str(raw_range): 
                guess = str(raw_range).split("-")[0].strip()
            elif raw_range: 
                guess = str(raw_range).strip()
            
            # Heuristic: Check Front Matter titles if guess isn't a digit
            if not guess.isdigit() or title.lower() in ["preface", "contents", "introduction", "foreword"]:
                found = find_best_match_for_title(title, page_map, page_order)
                if found: guess = found
            
            # Convert that Label -> Physical Index in the file
            try:
                # If the page exists, get its index (0 to N)
                idx = page_order.index(guess)
            except ValueError:
                # If TOC page doesn't exist in file, default to 0 (Start of file)
                idx = 0
                
            indices[i] = idx
        st.session_state["splitter_indices"] = indices

    # 3. Helper to adjust index (Previous/Next)
    def adjust_index(i, direction):
        curr = st.session_state["splitter_indices"][i]
        new = curr + direction
        # Ensure we don't go out of bounds
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
            # Grab content for preview
            preview_text = page_map.get(current_label, "Error: Content missing")
            
            # FIXED: Added current_idx to the key. 
            # This forces Streamlit to treat it as a new widget and update the value when the page changes.
            st.text_area("Preview", value=preview_text[:400]+"...", height=120, key=f"pview_{i}_{current_idx}", disabled=True)
            
        st.divider()

    # 5. Actions
    c_back, c_run = st.columns([1, 4])
    with c_back:
        if st.button("⬅️ Back"):
            st.session_state.pipeline_stage = "proof"
            st.rerun()
            
    with c_run:
        # --- PART A: SPLIT & UPLOAD ---
        if st.button("✂️ Split & Upload to Bahai.works", type="primary"):
            target_base = st.session_state["target_page"]
            
            # 1. Reconstruct the Access Header
            access_group = target_base.replace(" ", "")
            header_content = f"<accesscontrol>Access:{access_group}</accesscontrol>{{{{Publicationinfo}}}}\n"
            
            progress_bar = st.progress(0)
            status_box = st.empty()
            
            try:
                # 2. Build the final cut-list based on the verified INDICES
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

                # 3. Process splits
                for i, chapter in enumerate(final_split_data):
                    ch_title = chapter['title']
                    ch_page_name = chapter['page_name']
                    start_idx = chapter['start_idx']
                    
                    # End index is the start of the next chapter
                    if i + 1 < len(final_split_data):
                        end_idx = final_split_data[i+1]['start_idx']
                    else:
                        end_idx = len(page_order)
                    
                    status_box.write(f"Processing {ch_title}...")
                    
                    # 4. Concatenate text
                    raw_text = ""
                    for p_idx in range(start_idx, end_idx):
                        p_label = page_order[p_idx]
                        raw_text += page_map[p_label]
                    
                    # 5. Combine Header + Text
                    full_content = header_content + raw_text
                    
                    # 6. Upload
                    full_title = f"{target_base}/{ch_page_name}"
                    upload_to_bahaiworks(full_title, full_content, "Splitter Upload")
                    
                    progress_bar.progress((i + 1) / len(final_split_data))
                    
                status_box.success("✅ All chapters split and uploaded (headers preserved)!")
                st.balloons()
                st.session_state["split_completed"] = True
                
            except Exception as e:
                st.error(f"Failed: {e}")

        # --- PART B: HANDOFF TO CHAPTER MANAGER ---
        if st.session_state.get("split_completed"):
            st.divider()
            st.subheader("4. Chapter Metadata")
            
            # Filter for items that probably need metadata (Authors exists OR it's a sub-section)
            # We use the same list we used for splitting, or the full TOC if you prefer flexibility.
            # Here we pass the full TOC so you can decide in the next screen what to link.
            
            if st.button("📝 Review & Create Chapter Items", type="primary", width='stretch'):
                # 1. Pack the data
                chapter_payload = []
                full_toc = st.session_state.get("toc_map", [])
                
                for item in full_toc:
                    # Logic: Pre-fill only relevant items (e.g. those with authors or valid page names)
                    # You can adjust this filter, but sending everything allows you to delete rows in the next step.
                    if item.get("page_name") and str(item.get("page_name")).strip() != "":
                        chapter_payload.append(item)
                
                st.session_state["chapter_review_data"] = chapter_payload
                st.session_state["chapter_parent_qid"] = st.session_state.get("parent_qid", "")
                st.session_state["chapter_target_base"] = st.session_state.get("target_page", "")
                
                # 2. Switch Page
                st.switch_page("pages/05_chapter_items.py")
