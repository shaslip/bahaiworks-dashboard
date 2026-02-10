import streamlit as st
import os
import sys
import json
import re
import time
import fitz  # PyMuPDF
from PIL import Image
import io
import requests

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.gemini_processor import proofread_with_formatting
from src.mediawiki_uploader import upload_to_bahaiworks, API_URL

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

STATE_FILE = os.path.join(project_root, "automation_state.json")

st.set_page_config(page_title="Fully Automated Proofreader", page_icon="🤖", layout="wide")

# ==============================================================================
# 1. HELPER FUNCTIONS
# ==============================================================================

def generate_header(current_issue_num):
    """
    Generates the MediaWiki {{header}} template.
    Calculates Previous/Next links based on the current issue number.
    """
    try:
        curr = int(current_issue_num)
        prev_num = curr - 1
        next_num = curr + 1
        
        # Logic for first issue (No Previous)
        prev_link = f"[[../../Issue {prev_num}/Text|Previous]]" if prev_num > 0 else ""
        
        # We assume there is always a next issue for now
        next_link = f"[[../../Issue {next_num}/Text|Next]]"

        header = f"""{{{{header
 | title      = [[../../]]
 | author     = 
 | translator = 
 | section    = Issue {curr}
 | previous   = {prev_link}
 | next       = {next_link}
 | notes      = {{{{bnreturn}}}}{{{{ps|1}}}}
 | categories = 
}}}}
"""
        return header
    except ValueError:
        return "" # Fail gracefully if issue isn't a number

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
    Surgically replaces content FOLLOWING {{page|X...}} tag, preserving the tag itself.
    """
    # 1. Find the start of the specific page tag
    # Matches {{page|2}} or {{page|2|...}}
    pattern_tag_start = re.compile(r'\{\{page\s*\|\s*' + str(page_num) + r'(?:\||\}\})', re.IGNORECASE)
    match = pattern_tag_start.search(wikitext)
    
    if not match:
        return None, f"Tag {{page|{page_num}}} not found in live text."
        
    # 2. Find the CLOSING }} of this specific tag to ensure we don't cut off attributes
    # We search starting from the beginning of the match
    tag_start_index = match.start()
    
    # We need to find the FIRST "}}" that occurs after the start of the tag
    # This correctly handles {{page|2|file=foo.pdf}}
    tag_end_index = wikitext.find("}}", tag_start_index)
    
    if tag_end_index == -1:
         return None, f"Malformed tag: {{page|{page_num}}} has no closing '}}'."
         
    # The content starts AFTER the closing brackets }} (index + 2 chars)
    content_start_pos = tag_end_index + 2
    
    # 3. Find the START of the NEXT page tag to define the end of content
    pattern_next = re.compile(r'\{\{page\s*\|')
    match_next = pattern_next.search(wikitext, content_start_pos)
    
    content_end_pos = match_next.start() if match_next else len(wikitext)
    
    # 4. Splice
    new_wikitext = wikitext[:content_start_pos] + "\n" + new_content.strip() + "\n" + wikitext[content_end_pos:]
    
    return new_wikitext, None

def cleanup_page_seams(wikitext):
    """
    Fixes text artifacts at page boundaries safely.
    """
    
    # 1. Fix Hyphenated Words (Specific Case: word- \n {{page}} \n suffix)
    # Move {{page}} before the word, remove hyphen, join suffix.
    # Ex: participat- \n {{page}} \n ing  ->  {{page}}participating
    wikitext = re.sub(
        r'([a-zA-Z]+)-\s*\n\s*(\{\{page\|[^}]+\}\})\s*\n\s*([a-z]+)',
        r'\2\1\3',
        wikitext
    )

    # 2. Fix Sentence Flow (General Case: {{page}} \n Word)
    # Remove the newline after {{page}} ONLY if the next line is text.
    # SAFETY: We uses (?!...) to ensure we do NOT match if the next char is 
    # '{|' (table), '!' (table header), '|' (table row), '=' (header), or '*'/'#' (lists).
    wikitext = re.sub(
        r'(\{\{page\|[^}]+\}\})\n(?![{|!=*#])',
        r'\1',
        wikitext
    )
    
    return wikitext

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
# 3. UI & MAIN LOGIC
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
    stop_info = st.button("🛑 Stop (Finish Current Page)", use_container_width=True)

# 4. Automation Loop
if start_btn:
    current_idx = state['current_file_index']
    
    # If resuming, we start from the file index in state
    # If "Test Mode" is on, we only loop ONCE.
    end_idx = current_idx + 1 if run_mode.startswith("Test") else total_files

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
            # A. Get Image
            with st.spinner(f"📄 Reading Page {page_num}..."):
                img = get_page_image_data(pdf_path, page_num)
                
            if img is None:
                log_area.text(f"Finished {short_name}. Next file.")
                break
            
            try:
                # B. Gemini Processing
                log_area.text(f"✨ Gemini is proofreading Page {page_num}...")
                new_text = proofread_with_formatting(img)
                
                if "GEMINI_ERROR" in new_text:
                    raise Exception(new_text)

                # --- HEADER INJECTION (Page 1 Only) ---
                if page_num == 1:
                    # Extract issue number from filename (e.g. US_Supplement_1.pdf -> 1)
                    match = re.search(r'(\d+)', short_name)
                    if match:
                        issue_num = match.group(1)
                        header = generate_header(issue_num)
                        new_text = header + "\n" + new_text

                # --- NOTOC INJECTION (Last Page Only) ---
                # We need to know if this is the last page. 
                # fitz (PyMuPDF) lets us check page count.
                doc = fitz.open(pdf_path)
                is_last_page = (page_num == len(doc))
                doc.close()

                if is_last_page:
                    new_text = new_text + "\n__NOTOC__"

                # C. Fetch Live Wiki Text
                log_area.text(f"🌐 Fetching live text from {wiki_title}...")
                current_wikitext, error = fetch_wikitext(wiki_title)
                
                if error:
                    st.error(f"CRITICAL ERROR: Could not fetch '{wiki_title}'. Does the page exist?")
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

                # --- SAFE FINAL CLEANUP (Run only on the last page) ---
                if is_last_page:
                    log_area.text(f"🧹 Running final seam cleanup on {short_name}...")
                    
                    # 1. Fetch the FRESH full document text (Avoids NameError)
                    full_text, fetch_err = fetch_wikitext(wiki_title)
                    
                    if not fetch_err and full_text:
                        # 2. Run the SAFE regex fixes
                        cleaned_text = cleanup_page_seams(full_text)
                        
                        # 3. Save again ONLY if changes were made
                        if cleaned_text != full_text:
                            cleanup_res = upload_to_bahaiworks(wiki_title, cleaned_text, "Automated Cleanup: Seams & Hyphens")
                            if cleanup_res.get('edit', {}).get('result') == 'Success':
                                log_area.text(f"✨ Cleanup saved successfully!")
                            else:
                                st.error(f"Cleanup Save Failed: {cleanup_res}")
                # ----------------------------------------------------

                # F. Update State (Success)
                # We save the NEXT page number as the resume point
                save_state(i, page_num + 1, "running", last_file_path=short_name)
                
                with status_container:
                    st.success(f"✅ Saved Page {page_num}")
                
                page_num += 1
                time.sleep(1) # Polite API delay

            except Exception as e:
                st.error(f"🚨 EXCEPTION OCCURRED: {str(e)}")
                st.stop()

    # End of Loop
    st.success("🎉 Batch Processing Complete!")
    
    # FIX: Save the index of the NEXT file so we can resume later
    # If we just finished file 'i', we want to start at 'i + 1' next time
    # 'end_idx' holds the value of the next file index in the logic
    save_state(end_idx, 1, "done", last_file_path=short_name)
