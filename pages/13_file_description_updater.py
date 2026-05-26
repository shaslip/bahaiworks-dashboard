import streamlit as st
import os
import sys
import requests
import concurrent.futures

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
if "files_data" not in st.session_state:
    st.session_state.files_data = {}  # {title: {"original": text, "new": text}}
if "processing_complete" not in st.session_state:
    st.session_state.processing_complete = False
if "target_category" not in st.session_state:
    st.session_state.target_category = ""

st.title("🖼️ File Description Updater (Bahai.media)")
st.markdown("Fetch files from a category, reformat their descriptions using Gemini, and upload changes.")

# --- Inputs ---
category_input = st.text_input("Category Name", value="Category:Baha'i News No 486", help="e.g., Category:Baha'i News No 486")

def process_single_file(title, wikitext, target_cat):
    """Worker function for threading"""
    new_text = format_file_description(wikitext, target_cat)
    return title, new_text

if st.button("Fetch & Process", type="primary"):
    if not category_input:
        st.warning("Please enter a category name.")
        st.stop()

    st.session_state.target_category = category_input
    st.session_state.files_data = {}
    st.session_state.processing_complete = False

    session = requests.Session()
    
    with st.spinner(f"Fetching non-PDF files from {category_input}..."):
        files = get_category_files(category_input, session=session, api_url=MEDIA_API_URL)
    
    if not files:
        st.error("No non-PDF files found in this category.")
        st.stop()
        
    st.info(f"Found {len(files)} files. Fetching wikitext and processing with Gemini (up to 50 concurrent workers)...")
    
    # Pre-fetch all wikitexts sequentially (fast enough, avoids API block on simple GET)
    raw_texts = {}
    progress_bar = st.progress(0)
    for i, title in enumerate(files):
        text, err = fetch_wikitext(title, session=session, api_url=MEDIA_API_URL)
        if text:
            raw_texts[title] = text
        progress_bar.progress((i + 1) / len(files))
    
    # Process with Gemini Concurrently
    processed_count = 0
    progress_bar.empty()
    st.write("🤖 Gemini Processing...")
    gemini_progress = st.progress(0)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        # Submit all tasks
        future_to_title = {
            executor.submit(process_single_file, title, text, category_input): title 
            for title, text in raw_texts.items()
        }
        
        # Gather results as they complete
        for future in concurrent.futures.as_completed(future_to_title):
            title = future_to_title[future]
            try:
                title_result, new_text = future.result()
                st.session_state.files_data[title] = {
                    "original": raw_texts[title],
                    "new": new_text
                }
            except Exception as exc:
                st.error(f"Error processing {title}: {exc}")
                
            processed_count += 1
            gemini_progress.progress(processed_count / len(raw_texts))
            
    st.session_state.processing_complete = True
    st.success("Processing complete! Review changes below.")
    st.rerun()

# --- Display Results & Editing ---
if st.session_state.processing_complete and st.session_state.files_data:
    st.divider()
    st.subheader("Review and Edit Descriptions")
    
    # Keep track of edits directly in session_state via the text_area key
    for title, data in st.session_state.files_data.items():
        st.markdown(f"### [{title}](https://bahai.media/{title.replace(' ', '_')})")
        col1, col2 = st.columns(2)
        
        with col1:
            st.text_area("Original Wikitext", value=data["original"], height=250, disabled=True, key=f"orig_{title}")
            
        with col2:
            # The user can edit this box. We use the 'new' text as the initial value.
            # Changes are automatically saved to st.session_state[f"edit_{title}"]
            st.text_area("New Wikitext (Editable)", value=data["new"], height=250, key=f"edit_{title}")
            
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
            st.session_state.files_data = {}
            st.session_state.processing_complete = False
            if st.button("Start Over"):
                st.rerun()
