import streamlit as st
import os
import sys
import requests
import concurrent.futures
import time
import re
import streamlit.components.v1 as components

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
if "file_categories" not in st.session_state:
    st.session_state.file_categories = {} # Maps title -> specific category it was found in
if "files_data" not in st.session_state:
    st.session_state.files_data = {}
if "target_category" not in st.session_state:
    st.session_state.target_category = ""

# Callback to fully reset the app state
def reset_app():
    st.session_state.step = 0
    st.session_state.raw_texts = {}
    st.session_state.file_categories = {}
    st.session_state.files_data = {}
    st.session_state.target_category = ""

st.title("🖼️ File Description Updater (Bahai.media)")
st.markdown("Fetch files from a category, reformat their descriptions using Gemini, and upload changes.")

def process_single_file(title, wikitext, target_cat):
    """Worker function for threading"""
    new_text = format_file_description(wikitext, target_cat)
    return title, new_text

def scroll_to_top():
    """Forces the Streamlit iframe parent to scroll to the top."""
    js = '''
    <script>
        // 1. Try scrolling the main window
        window.parent.scrollTo(0,0);
        
        // 2. Try scrolling Streamlit's main app container (newer versions)
        var stMain = window.parent.document.querySelector('[data-testid="stMain"]');
        if (stMain) { stMain.scrollTo(0,0); }
        
        // 3. Try scrolling Streamlit's older app container
        var main = window.parent.document.querySelector('section.main');
        if (main) { main.scrollTo(0,0); }
    </script>
    '''
    components.html(js, height=0)

def is_already_formatted(wikitext):
    """Check if the wikitext is already formatted with == File info == and {{cs}}."""
    has_file_info = bool(re.search(r'==\s*File info\s*==', wikitext, re.IGNORECASE))
    has_cs = bool(re.search(r'\{\{\s*cs\b', wikitext, re.IGNORECASE))
    return has_file_info and has_cs

# ==========================================
# STEP 0: FETCH FILES
# ==========================================
if st.session_state.step == 0:
    tab1, tab2 = st.tabs(["📁 Single Category", "📚 Sequential Categories (Supercharged)"])
    
    # --- TAB 1: SINGLE CATEGORY ---
    with tab1:
        category_input = st.text_input(
            "Category Name", 
            value=st.session_state.target_category, 
            placeholder="e.g., Category:Baha'i News No 486",
            key="single_cat_input"
        )
        
        if st.button("Fetch Files", type="primary", key="btn_single_fetch"):
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
            file_cats = {}
            progress_bar = st.progress(0)
            for i, title in enumerate(files):
                text, err = fetch_wikitext(title, session=session, api_url=MEDIA_API_URL)
                # Only add if it's not already formatted
                if text and not is_already_formatted(text):
                    raw_texts[title] = text
                    file_cats[title] = category_input
                progress_bar.progress((i + 1) / len(files))
                
            if not raw_texts:
                st.success("All files in this category are already formatted! Nothing to do.")
                st.stop()
                
            st.session_state.raw_texts = raw_texts
            st.session_state.file_categories = file_cats
            st.session_state.target_category = category_input
            st.session_state.step = 1
            st.rerun()

    # --- TAB 2: SEQUENTIAL CATEGORIES ---
    with tab2:
        st.info("Start at a base category (e.g., 'BWNS 405') and automatically fetch the next categories (406, 407...) until the target unformatted image count is reached.")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            seq_category_input = st.text_input(
                "Starting Category Name", 
                value=st.session_state.target_category, 
                placeholder="e.g., Category:BWNS 405",
                key="seq_cat_input"
            )
        with col2:
            target_count = st.number_input("Target Total Images", min_value=5, max_value=200, value=50, step=5)
            
        if st.button("Fetch Sequential Files", type="primary", key="btn_seq_fetch"):
            if not seq_category_input:
                st.warning("Please enter a starting category name.")
                st.stop()
                
            # Regex to find the LAST number in the string (e.g., "Category:1992 BWNS 405" -> "405")
            # \D*$ ensures there are no digits after the one we matched.
            match = re.search(r'^(.*?)(\d+)(\D*)$', seq_category_input)
            if not match:
                st.error("Could not find a number in the category name to sequence. Please use the Single Category tab.")
                st.stop()
                
            prefix, num_str, suffix = match.groups()
            current_num = int(num_str)
            
            session = requests.Session()
            raw_texts = {}
            file_cats = {}
            consecutive_empty = 0
            max_empty = 15 # Stop if we hit 15 empty categories in a row
            
            status_text = st.empty()
            progress_bar = st.progress(0)
            
            # Loop until we hit the target count or run out of contiguous categories
            while len(raw_texts) < target_count and consecutive_empty < max_empty:
                current_cat = f"{prefix}{current_num}{suffix}"
                status_text.info(f"Searching {current_cat}... (Found {len(raw_texts)}/{target_count} unformatted images)")
                
                files = get_category_files(current_cat, session=session, api_url=MEDIA_API_URL)
                if files:
                    consecutive_empty = 0
                    for title in files:
                        if len(raw_texts) >= target_count:
                            break # Stop if we hit the target exactly
                            
                        text, err = fetch_wikitext(title, session=session, api_url=MEDIA_API_URL)
                        # Only add if it's not already formatted
                        if text and not is_already_formatted(text):
                            raw_texts[title] = text
                            file_cats[title] = current_cat
                            
                        # Update progress bar based on target count
                        progress_bar.progress(min(len(raw_texts) / target_count, 1.0))
                else:
                    consecutive_empty += 1
                    
                current_num += 1
                
            if not raw_texts:
                st.error("No unformatted files found in the starting category or the subsequent sequences.")
                st.stop()
                
            status_text.success(f"Finished searching. Found {len(raw_texts)} unformatted files across {current_num - int(num_str)} categories.")
            time.sleep(1)
            
            st.session_state.raw_texts = raw_texts
            st.session_state.file_categories = file_cats
            st.session_state.target_category = seq_category_input # Base category for reference
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
        if title_to_remove in st.session_state.file_categories:
            del st.session_state.file_categories[title_to_remove]

    if not st.session_state.raw_texts:
        st.warning("No files left in the queue.")
    else:
        for title, text in list(st.session_state.raw_texts.items()):
            file_cat = st.session_state.file_categories.get(title, "Unknown Category")
            st.markdown(f"**{title}** *(From: {file_cat})*")
            
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
        if st.button("Cancel / Start Over", on_click=reset_app):
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
                # Pass the specific category for that file so format_file_description strips the right category
                future_to_title = {
                    executor.submit(
                        process_single_file, 
                        title, 
                        st.session_state.raw_texts[title], 
                        st.session_state.file_categories.get(title, st.session_state.target_category)
                    ): title 
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
    scroll_to_top()
    
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
            # Clear state on full success via callback to avoid nested button issues
            st.button("Start Over", on_click=reset_app)
