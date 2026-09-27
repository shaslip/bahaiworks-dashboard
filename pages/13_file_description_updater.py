import streamlit as st
import os
import sys
import requests
import concurrent.futures
import time

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from src.mediawiki_uploader import get_category_files, fetch_wikitext, upload_to_mediawiki
from src.gemini_processor import format_file_description

MEDIA_API_URL = 'https://bahai.media/api.php'

st.set_page_config(page_title="File Description Updater", page_icon="🖼️", layout="wide")

# --- State Initialization ---
if "step" not in st.session_state:
    st.session_state.step = 0  # 0: Fetch, 1: Review Queue, 2: Review Edits
if "raw_texts" not in st.session_state:
    st.session_state.raw_texts = {}
if "files_data" not in st.session_state:
    st.session_state.files_data = {}
if "target_category" not in st.session_state:
    st.session_state.target_category = ""

st.title("🖼️ File Description Updater (Bahai.media)")
st.markdown("Fetch files from a category, reformat their descriptions using Gemini, and upload changes.")

def process_single_file(title, wikitext, target_cat):
    """Worker function for threading"""
    new_text = format_file_description(wikitext, target_cat)
    return title, new_text

# ==========================================
# STEP 0: FETCH FILES
# ==========================================
if st.session_state.step == 0:
    category_input = st.text_input("Category Name", value="Category:Baha'i News No 486", help="e.g., Category:Baha'i News No 486")
    
    if st.button("Fetch Files", type="primary"):
        if not category_input:
            st.warning("Please enter a category name.")
            st.stop()

        session = requests.Session()
        
        with st.spinner(f"Fetching non-PDF files from {category_input}..."):
            files = get_category_files(category_input, session=session, api_url=MEDIA_API_URL)
        
        if not files:
            st.error("No non-PDF files found in this category.")
            st.stop()
            
        st.info(f"Found {len(files)} files. Fetching original wikitexts...")
        
        raw_texts = {}
        progress_bar = st.progress(0)
        for i, title in enumerate(files):
            text, err = fetch_wikitext(title, session=session, api_url=MEDIA_API_URL)
            if text:
                raw_texts[title] = text
            progress_bar.progress((i + 1) / len(files))
            
        st.session_state.raw_texts = raw_texts
        st.session_state.target_category = category_input
        st.session_state.step = 1
        st.rerun()

# ==========================================
# STEP 1: REVIEW QUEUE & PROCESS
# ==========================================
if st.session_state.step == 1:
    st.subheader("1. Review Files")
    st.info("Review the fetched files below. Click 'Remove' on any files that do not need processing to save API costs.")
    
    # Callback to immediately remove a file from the queue
    def remove_from_queue(title_to_remove):
        if title_to_remove in st.session_state.raw_texts:
            del st.session_state.raw_texts[title_to_remove]

    if not st.session_state.raw_texts:
        st.warning("No files left in the queue.")
    else:
        for title, text in list(st.session_state.raw_texts.items()):
            st.markdown(f"**{title}**")
            
            # Using columns to constrain the width of the text box and align the button.
            # Ratios: 6 (Text Box), 1.5 (Button), 2.5 (Empty space to prevent full width)
            col_text, col_btn, col_spacer = st.columns([6, 1.5, 2.5])
            
            with col_text:
                st.text_area("Raw Text", value=text, height=200, disabled=True, label_visibility="collapsed", key=f"view_{title}")
                
            with col_btn:
                st.button("❌ Remove", key=f"btn_remove_{title}", on_click=remove_from_queue, args=(title,), use_container_width=True)
            
            st.divider()
            
    col1, col2 = st.columns([1, 5])
    with col1:
        if st.button("Cancel / Start Over"):
            st.session_state.step = 0
            st.session_state.raw_texts = {}
            st.rerun()
            
    with col2:
        # Process whatever remains in the raw_texts dictionary
        if st.button("🤖 Process Remaining Files", type="primary", disabled=len(st.session_state.raw_texts) == 0):
            st.write("🤖 Gemini Processing...")
            gemini_progress = st.progress(0)
            processed_count = 0
            
            st.session_state.files_data = {}
            titles_to_process = list(st.session_state.raw_texts.keys())
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                future_to_title = {
                    executor.submit(process_single_file, title, st.session_state.raw_texts[title], st.session_state.target_category): title 
                    for title in titles_to_process
                }
                
                for future in concurrent.futures.as_completed(future_to_title):
                    title = future_to_title[future]
                    try:
                        title_result, new_text = future.result()
                        st.session_state.files_data[title] = {
                            "original": st.session_state.raw_texts[title],
                            "new": new_text
                        }
                    except Exception as exc:
                        st.error(f"Error processing {title}: {exc}")
                        
                    processed_count += 1
                    gemini_progress.progress(processed_count / len(titles_to_process))
                    
            st.success("Processing complete!")
            time.sleep(1)
            st.session_state.step = 2
            st.rerun()

# ==========================================
# STEP 2: REVIEW EDITS & UPLOAD
# ==========================================
if st.session_state.step == 2:
    # --- Display Results & Editing ---
    st.subheader("2. Review and Edit Descriptions")
    
    if st.button("Back to Selection"):
        st.session_state.step = 1
        st.rerun()
        
    st.divider()
    
    # Keep track of edits directly in session_state via the text_area key
    for title, data in st.session_state.files_data.items():
        st.markdown(f"### [{title}](https://bahai.media/{title.replace(' ', '_')})")
        col1, col2 = st.columns(2)
        
        with col1:
            st.text_area("Original Wikitext", value=data["original"], height=400, disabled=True, key=f"orig_{title}")
            
        with col2:
            # The user can edit this box. We use the 'new' text as the initial value.
            # Changes are automatically saved to st.session_state[f"edit_{title}"]
            st.text_area("New Wikitext (Editable)", value=data["new"], height=400, key=f"edit_{title}")
            
        st.divider()

    # --- Upload Button ---
    if st.button("🚀 Upload Changes to Bahai.media", type="primary", use_container_width=True):
        session = requests.Session()
        success_count = 0
        error_count = 0
        
        progress_text = st.empty()
        upload_bar = st.progress(0)
        
        total_files = len(st.session_state.files_data)
        
        for i, title in enumerate(st.session_state.files_data.keys()):
            progress_text.text(f"Uploading {title} ({i+1}/{total_files})...")
            
            # Grab the potentially edited text from session state
            final_text = st.session_state[f"edit_{title}"]
            
            # Skip if no changes were made relative to the original
            if final_text.strip() == st.session_state.files_data[title]["original"].strip():
                st.info(f"Skipped {title} (No changes detected)")
                upload_bar.progress((i + 1) / total_files)
                continue
                
            try:
                upload_to_mediawiki(
                    title=title, 
                    content=final_text, 
                    summary="Updating file description layout & orthography", 
                    session=session, 
                    api_url=MEDIA_API_URL
                )
                success_count += 1
            except Exception as e:
                st.error(f"Failed to upload {title}: {e}")
                error_count += 1
                
            upload_bar.progress((i + 1) / total_files)
            
        st.success(f"Upload complete! Successfully updated {success_count} files. {error_count} errors.")
        
        if error_count == 0:
            # Clear state on full success
            if st.button("Start Over"):
                st.session_state.step = 0
                st.session_state.raw_texts = {}
                st.session_state.files_data = {}
                st.rerun()
