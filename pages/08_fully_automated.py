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
from src.gemini_processor import proofread_with_formatting, transcribe_with_document_ai, reformat_raw_text
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

def generate_header(current_issue_num, year=None):
    """
    Generates the MediaWiki {{header}} template.
    Supports single issues (64) and ranges (64-65).
    """
    try:
        # Check for range (e.g., "64-65")
        if '-' in str(current_issue_num):
            parts = str(current_issue_num).split('-')
            start_num = int(parts[0])
            end_num = int(parts[-1])
            curr_display = current_issue_num
            
            # Logic: Prev is start-1, Next is end+1
            prev_num = start_num - 1
            next_num = end_num + 1
        else:
            curr = int(current_issue_num)
            curr_display = str(curr)
            prev_num = curr - 1
            next_num = curr + 1
        
        prev_link = f"[[../../Issue {prev_num}/Text|Previous]]" if prev_num > 0 else ""
        next_link = f"[[../../Issue {next_num}/Text|Next]]"
        
        # Format categories
        cat_str = str(year) if year else ""

        header = f"""{{{{header
 | title      = [[../../]]
 | author     = 
 | translator = 
 | section    = Issue {curr_display}
 | previous   = {prev_link}
 | next       = {next_link}
 | notes      = {{{{bnreturn}}}}{{{{ps|1}}}}
 | categories = {cat_str}
}}}}
"""
        return header
    except ValueError:
        return ""

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

def inject_text_into_page(wikitext, page_num, new_content, pdf_filename):
    """
    Surgically replaces content FOLLOWING {{page|X...}} tag.
    
    NEW: If the tag does NOT exist, it appends the new page to the end of the file.
    """
    # 1. Try to find the existing tag
    pattern_tag_start = re.compile(r'\{\{page\s*\|\s*' + str(page_num) + r'(?:\||\}\})', re.IGNORECASE)
    match = pattern_tag_start.search(wikitext)
    
    if match:
        # --- EXISTING PAGE LOGIC ---
        # Find closing }}
        tag_start_index = match.start()
        tag_end_index = wikitext.find("}}", tag_start_index)
        
        if tag_end_index == -1:
             return None, f"Malformed tag: {{page|{page_num}}} has no closing '}}'."
             
        # Content starts after closing }}
        content_start_pos = tag_end_index + 2
        
        # Find start of NEXT tag to define end of content
        pattern_next = re.compile(r'\{\{page\s*\|')
        match_next = pattern_next.search(wikitext, content_start_pos)
        
        content_end_pos = match_next.start() if match_next else len(wikitext)
        
        # Splice
        new_wikitext = wikitext[:content_start_pos] + "\n" + new_content.strip() + "\n" + wikitext[content_end_pos:]
        return new_wikitext, None

    else:
        # --- NEW PAGE APPEND LOGIC ---
        # The tag doesn't exist. We assume this is a new page at the end of the document.
        # Check if we should append. Usually, if page_num is > 1, it's safe to append.
        
        # Construct the new tag
        # We use the filename from the script state to build {{page|X|file=...|page=X}}
        new_tag = f"{{{{page|{page_num}|file={pdf_filename}|page={page_num}}}}}"
        
        # Append to the end
        # Ensure there is a newline before the new page
        if not wikitext.endswith("\n"):
            wikitext += "\n"
            
        new_wikitext = wikitext + "\n" + new_tag + "\n" + new_content.strip()
        
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
    """Recursively finds all PDF files, ignoring those marked as '-old', and sorts them naturally."""
    pdf_files = []
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            # Check if it is a PDF and NOT an 'old' version
            if f.lower().endswith(".pdf") and "-old" not in f.lower():
                full_path = os.path.join(dirpath, f)
                pdf_files.append(full_path)
    
    # Natural sort (Issue 2 comes before Issue 10)
    pdf_files.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])
    return pdf_files

def get_wiki_title(local_path, root_folder, base_wiki_title):
    """
    Extracts the issue number from the filename to match Wiki format.
    Now supports ranges like '64-65'.
    """
    filename = os.path.basename(local_path)
    
    # UPDATED: Capture digits, optionally followed by hyphen and more digits
    match = re.search(r'(\d+(?:-\d+)?)', filename)
    
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

ocr_strategy = st.sidebar.radio(
    "OCR Strategy", 
    ["Gemini (Default)", "DocAI Only"], 
    help="DocAI Only skips Gemini and goes straight to Google Cloud Vision OCR."
)

st.sidebar.divider()

# Load State
state = load_state()

if st.sidebar.button("🗑️ Reset State (Start Over)"):
    state = reset_state()
    st.sidebar.success("State cleared.")
    st.rerun()

st.sidebar.markdown(f"**Current State:**\n- File Index: `{state['current_file_index']}`\n- Page: `{state['current_page_num']}`")

# --- Manual State Modification ---
with st.sidebar.expander("🔧 Modify Position"):
    # Input fields initialized with current state values
    new_file_index = st.number_input("File Index (0-based)", min_value=0, value=state['current_file_index'])
    new_page_num = st.number_input("Page Number", min_value=1, value=state['current_page_num'])
    
    if st.button("Update State"):
        # Update the JSON file with your manual values
        save_state(new_file_index, new_page_num, "manual_override", last_file_path=state.get('last_processed'))
        st.sidebar.success("State updated!")
        time.sleep(0.5) 
        st.rerun()

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

    # --- Failure Tracking ---
    consecutive_page1_failures = 0
    global_fallback_active = False

    for i in range(current_idx, end_idx):
        pdf_path = pdf_files[i]
        
        # 4a. Resolve Titles
        wiki_title = get_wiki_title(pdf_path, input_folder, base_title)
        short_name = os.path.basename(pdf_path)
        
        status_container.markdown(f"### 🔨 Processing File {i+1}/{total_files}: `{short_name}`")
        status_container.caption(f"Target Wiki Page: `{wiki_title}`")
        
        # 4b. Resume Page Logic
        if i == state['current_file_index']:
            start_page = state['current_page_num']
        else:
            start_page = 1
            
        # --- Fallback Flag Logic ---
        # If we hit 5 successive Page 1 failures, we force fallback for everything.
        if global_fallback_active:
             fallback_enabled = True
        else:
             fallback_enabled = False

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
                # --- CHANGED: Strategy Logic ---
                final_text = ""
                use_docai_now = (ocr_strategy == "DocAI Only") or fallback_enabled

                if use_docai_now:
                    log_area.text(f"🤖 [DocAI Mode] OCR Page {page_num}...")
                    raw_ocr = transcribe_with_document_ai(img)
                    
                    if "DOCAI_ERROR" in raw_ocr:
                        st.error(f"DocAI Failed: {raw_ocr}")
                        st.stop()
                    
                    log_area.text(f"🎨 [DocAI Mode] Formatting Page {page_num}...")
                    final_text = reformat_raw_text(raw_ocr)

                    if "FORMATTING_ERROR" in final_text:
                        # Log warning instead of Error/Stop
                        st.warning(f"⚠️ DocAI Reformatter Failed on Page {page_num}. Skipping page. (Error: {final_text[:200]}...)")
                        
                        # Save state so we can resume from the NEXT page if the script stops later
                        save_state(i, page_num + 1, "running", last_file_path=short_name)
                        
                        # Increment and skip to next iteration
                        page_num += 1
                        continue
                
                else:
                    # Standard Gemini Routine
                    log_area.text(f"✨ Gemini processing Page {page_num}...")
                    final_text = proofread_with_formatting(img)

                # --- CHANGED: Error Handling (Don't swallow errors) ---
                if "GEMINI_ERROR" in final_text:
                    if "Recitation" in final_text or "Copyright" in final_text:
                        st.warning(f"⚠️ Copyright block on Page {page_num}. Engaging Fallback.")
                        
                        # --- NEW: Track Consecutive Page 1 Failures ---
                        if page_num == 1:
                            consecutive_page1_failures += 1
                            if consecutive_page1_failures >= 5:
                                global_fallback_active = True
                                st.error("🚨 5 successive Page 1 failures detected. Switching to Document AI for all remaining files.")
                        
                        # Activate Fallback
                        fallback_enabled = True
                        
                        # RETRY immediately with Fallback Routine
                        log_area.text(f"🔄 Retrying Page {page_num} with DocAI + Reformatter...")
                        
                        # Step 1: DocAI
                        raw_ocr = transcribe_with_document_ai(img)
                        if "DOCAI_ERROR" in raw_ocr:
                            st.error("Fallback OCR failed.")
                            st.stop()
                            
                        # Step 2: Gemini Text-to-Text Formatting
                        final_text = reformat_raw_text(raw_ocr)
                        
                    else:
                        st.error(f"🛑 CRITICAL API ERROR on Page {page_num}: {final_text}")
                        st.stop()
                else:
                    # --- NEW: Success Case ---
                    # If Page 1 succeeded with Gemini, reset the failure counter
                    if page_num == 1:
                        consecutive_page1_failures = 0

                # 3. Last Page Check (Add NOTOC)
                doc = fitz.open(pdf_path)
                is_last_page = (page_num == len(doc))
                doc.close()
                
                if is_last_page:
                    final_text += "\n__NOTOC__"

                # C. Fetch Live Wiki Text
                log_area.text(f"🌐 Fetching live text from {wiki_title}...")
                current_wikitext, error = fetch_wikitext(wiki_title)
                
                if error:
                    st.error(f"CRITICAL ERROR: Could not fetch '{wiki_title}'. Does the page exist?")
                    st.stop()
                
                # --- PAGE 1 SPECIAL HANDLING (Header & OCR Removal) ---
                if page_num == 1:
                    # 1. Remove {{ocr}} tags
                    current_wikitext = re.sub(r'\{\{ocr.*?\}\}\n?', '', current_wikitext, flags=re.IGNORECASE)

                    # 2. Extract Year from [[Category:YYYY]] (Read-only)
                    found_year = None
                    cat_match = re.search(r'\[\[Category:\s*(\d{4})\s*\]\]', current_wikitext, re.IGNORECASE)
                    
                    if cat_match:
                        found_year = cat_match.group(1)

                    # 3. Generate and Prepend Header
                    match = re.search(r'(\d+(?:-\d+)?)', short_name)
                    if match:
                        issue_num = match.group(1)
                        # Pass the found year to the header generator
                        header = generate_header(issue_num, year=found_year)
                        
                        # Prepend if missing
                        if "{{header" not in current_wikitext:
                            current_wikitext = header + "\n" + current_wikitext.lstrip()
                
                # D. Inject Content
                log_area.text(f"💉 Injecting content into {{page|{page_num}}}...")
                # FIXED: Changed 'new_text' to 'final_text'
                final_wikitext, inject_error = inject_text_into_page(current_wikitext, page_num, final_text, short_name)
                
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
