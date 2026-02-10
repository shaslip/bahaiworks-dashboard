import streamlit as st
import os
import sys
import json
import re
import time
import fitz  # PyMuPDF
import google.generativeai as genai
from PIL import Image
import io
import requests

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from src.gemini_processor import proofread_with_formatting
from src.mediawiki_uploader import upload_to_bahaiworks, API_URL

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

STATE_FILE = os.path.join(project_root, "automation_state.json")

st.set_page_config(page_title="Fully Automated Proofreader", page_icon="🤖", layout="wide")

# ==============================================================================
# 1. HELPER FUNCTIONS
# ==============================================================================

def fetch_wikitext(title):
    """
    Fetches the absolute latest revision of a page from the live Wiki.
    
    Returns: 
        (content, error_message)
    """
    try:
        # User-Agent header is often required to avoid 403 blocks from Wiki APIs
        headers = {"User-Agent": "BahaiWorksDashboard/1.0 (internal tool)"}
        params = {
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvprop": "content",
            "format": "json",
            "rvslots": "main"
        }
        
        response = requests.get(API_URL, params=params, headers=headers, timeout=10)
        data = response.json()
        
        pages = data.get('query', {}).get('pages', {})
        for pid in pages:
            # MediaWiki returns "-1" if the page is missing
            if pid == "-1":
                return None, f"Page '{title}' does not exist (ID -1)."
            
            # Extract the raw wikitext from the main slot
            return pages[pid]['revisions'][0]['slots']['main']['*'], None
            
    except Exception as e:
        return None, str(e)
    
    return None, "Unknown Error"


def inject_text_into_page(wikitext, page_num, new_content):
    """
    Surgically replaces content between {{page|X}} tags.
    
    1. Finds {{page|X|...}}
    2. Finds the START of the NEXT {{page|...}} tag (or end of file).
    3. Replaces everything in between with `new_content`.
    """
    # Regex matches: {{page | 19 }} OR {{page | 19 | file=... }}
    # We use re.IGNORECASE to handle variations like {{Page...}}
    pattern_start = re.compile(r'(\{\{page\s*\|\s*' + str(page_num) + r'(?:\||\}\}))', re.IGNORECASE)
    match_start = pattern_start.search(wikitext)
    
    if not match_start:
        return None, f"Tag {{page|{page_num}}} not found in live text."
        
    # The insertion point starts immediately after the closing brackets/pipe of the first tag
    start_pos = match_start.end()
    
    # Find the START of the NEXT page tag to define the boundary
    pattern_next = re.compile(r'\{\{page\s*\|')
    match_next = pattern_next.search(wikitext, start_pos)
    
    # If no next tag, assume we replace until the end of the document
    end_pos = match_next.start() if match_next else len(wikitext)
    
    # Construct the new string
    new_wikitext = wikitext[:start_pos] + "\n" + new_content.strip() + "\n" + wikitext[end_pos:]
    return new_wikitext, None

# ==============================================================================
# 2. STATE MANAGEMENT
# ==============================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"current_file_index": 0, "current_page_num": 1, "status": "idle", "last_processed": None}

def save_state(file_index, page_num, status, last_file_path=None):
    state = {
        "current_file_index": file_index,
        "current_page_num": page_num,
        "status": status,
        "last_processed": last_file_path
    }
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def reset_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    return {"current_file_index": 0, "current_page_num": 1, "status": "idle", "last_processed": None}

# ==============================================================================
# 3. HELPER FUNCTIONS
# ==============================================================================

def get_all_pdf_files(root_folder):
    """Recursively finds all PDF files and sorts them naturally."""
    pdf_files = []
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower().endswith(".pdf"):
                full_path = os.path.join(dirpath, f)
                pdf_files.append(full_path)
    
    # Natural sort (Issue 2 comes before Issue 10)
    pdf_files.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])
    return pdf_files

def get_wiki_title(local_path, root_folder, base_wiki_title):
    """
    Extracts the issue number from the filename to match Wiki format.
    Ex: 'US_Supplement_1.pdf' -> 'U.S._Supplement/Issue_1/Text'
    """
    filename = os.path.basename(local_path)
    
    # Extract the number from the filename (e.g., "1" from "US_Supplement_1")
    match = re.search(r'(\d+)', filename)
    
    if match:
        issue_num = match.group(1)
        # Construct standard Wiki format: Base / Issue_X / Text
        return f"{base_wiki_title}/Issue_{issue_num}/Text"
    
    # Fallback: Use full filename if no number is found
    clean_name = os.path.splitext(filename)[0]
    return f"{base_wiki_title}/{clean_name}/Text"

def get_page_image_data(pdf_path, page_num_1_based):
    doc = fitz.open(pdf_path)
    # PyMuPDF is 0-based
    if page_num_1_based > len(doc):
        doc.close()
        return None  # End of file
    
    page = doc.load_page(page_num_1_based - 1)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    doc.close()
    return img

# ==============================================================================
# 4. UI & MAIN LOGIC
# ==============================================================================

st.title("🤖 Fully Automated Periodical Processor")

# --- Sidebar Controls ---
st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Folder", value="/media/sarah/4TB/Projects/Bahai.works/English/U.S._Supplement")
base_title = st.sidebar.text_input("Base Wiki Title", value="U.S._Supplement")

run_mode = st.sidebar.radio("Run Mode", ["Test (1 PDF Only)", "Production (All PDFs)"])

st.sidebar.divider()

# Load State
state = load_state()

if st.sidebar.button("🗑️ Reset State (Start Over)"):
    state = reset_state()
    st.sidebar.success("State cleared.")
    st.rerun()

st.sidebar.markdown(f"**Current State:**\n- File Index: `{state['current_file_index']}`\n- Page: `{state['current_page_num']}`")

# --- Main Area ---

if not input_folder or not os.path.exists(input_folder):
    st.warning("Please provide a valid local folder path.")
    st.stop()

# 1. Scan Files
pdf_files = get_all_pdf_files(input_folder)
total_files = len(pdf_files)

if total_files == 0:
    st.error("No PDF files found in the directory.")
    st.stop()

st.info(f"📂 Found {total_files} PDF files in `{input_folder}`")

# 2. Status Display
status_container = st.container(border=True)
log_area = st.empty()

# 3. Action Buttons
col1, col2 = st.columns(2)
with col1:
    start_btn = st.button("🚀 Start / Resume Automation", type="primary", use_container_width=True)
with col2:
    # Stop is handled by Streamlit's native "Stop" button mostly, 
    # but we add this to visually indicate intent.
    stop_info = st.button("🛑 Stop (Finish Current Page)", use_container_width=True)

# 4. Automation Loop
if start_btn:
    current_idx = state['current_file_index']
    
    # If resuming, we start from the file index in state
    # If "Test Mode" is on, we only loop ONCE.
    end_idx = current_idx + 1 if run_mode.startswith("Test") else total_files

    progress_bar = st.progress(0)
    
    for i in range(current_idx, end_idx):
        pdf_path = pdf_files[i]
        
        # 4a. Resolve Titles
        wiki_title = get_wiki_title(pdf_path, input_folder, base_title)
        short_name = os.path.basename(pdf_path)
        
        status_container.markdown(f"### 🔨 Processing File {i+1}/{total_files}: `{short_name}`")
        status_container.caption(f"Target Wiki Page: `{wiki_title}`")
        
        # 4b. Resume Page Logic
        # If we are on the same file as saved state, resume page. Else start at 1.
        if i == state['current_file_index']:
            start_page = state['current_page_num']
        else:
            start_page = 1
            
        # 4c. Iterate Pages in PDF
        page_num = start_page
        
        while True:
            # Check for Stop Request (Simulation)
            # In Streamlit, we can't easily detect a button press inside a loop without callbacks.
            # We rely on the user hitting "Stop" in the UI to kill the script.
            # Since we save state per page, this is safe.
            
            # A. Get Image
            with st.spinner(f"📄 Reading Page {page_num}..."):
                img = get_page_image_data(pdf_path, page_num)
                
            if img is None:
                # End of PDF reached
                log_area.text(f"Finished {short_name}. Moving to next file.")
                break
            
            try:
                # B. Gemini Processing
                log_area.text(f"✨ Gemini is proofreading Page {page_num}...")
                new_text = proofread_with_formatting(img)
                
                if "GEMINI_ERROR" in new_text:
                    raise Exception(new_text)

                # C. Fetch Live Wiki Text
                log_area.text(f"🌐 Fetching live text from {wiki_title}...")
                current_wikitext, error = fetch_wikitext(wiki_title)
                
                if error:
                    # If page doesn't exist, we can't edit it. DIE.
                    st.error(f"CRITICAL ERROR: Could not fetch '{wiki_title}'. Does the page exist?")
                    st.error(f"API Response: {error}")
                    st.stop()
                
                # D. Inject Content
                log_area.text(f"💉 Injecting content into {{page|{page_num}}}...")
                final_wikitext, inject_error = inject_text_into_page(current_wikitext, page_num, new_text)
                
                if inject_error:
                    st.error(f"CRITICAL ERROR on {short_name} Page {page_num}: {inject_error}")
                    st.stop()

                # E. Upload
                log_area.text(f"💾 Saving to Bahai.works...")
                summary = f"Automated Proofread: {short_name} Pg {page_num}"
                res = upload_to_bahaiworks(wiki_title, final_wikitext, summary)
                
                if res.get('edit', {}).get('result') != 'Success':
                    st.error(f"UPLOAD FAILED: {res}")
                    st.stop()

                # F. Update State (Success)
                # We save the NEXT page number as the resume point
                save_state(i, page_num + 1, "running", last_file_path=short_name)
                
                # Visual Feedback
                with status_container:
                    st.success(f"✅ Saved Page {page_num}")
                
                page_num += 1
                time.sleep(1) # Polite API delay

            except Exception as e:
                st.error(f"🚨 EXCEPTION OCCURRED: {str(e)}")
                st.error("Script halted to prevent damage. Fix the error and click Start to resume.")
                st.stop()

    # End of Loop
    st.success("🎉 Batch Processing Complete!")
    # Reset state or mark as done
    save_state(0, 1, "done")
