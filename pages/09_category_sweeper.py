import streamlit as st
import os
import sys
import json
import re
import time
import requests
import fitz  # PyMuPDF
from PIL import Image
import io

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.gemini_processor import proofread_with_formatting, transcribe_with_document_ai, reformat_raw_text
from src.mediawiki_uploader import upload_to_bahaiworks, API_URL, fetch_wikitext, inject_text_into_page

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

STATE_FILE = os.path.join(project_root, "category_sweeper_state.json")

# Titles (Periodicals/Reports) to skip automatically
EXCLUDED_TITLES = [
    "The American Bahá’í",
    "Annual Reports",
    "Australian Baha’i Report",
    "Bahai Bulletin",
    "Bahai News India",
    "Bahá’í Canada",
    "Bahá’í Journal",
    "Bahá’í News Bulletin",
    "Bahá’í World",
    "Bahá’í Youth Bulletin",
    "Brilliant Star",
    "Bulletin",
    "Canadian Bahá’í News",
    "Child's Way",
    "Dialogue",
    "Herald of the South",
    "Light of the Pacific",
    "Living Nation",
    "Malaysian Bahá’í News",
    "Najm-i-Bák̲h̲tar",
    "National Bahá’í Review",
    "National Teaching Committee Bulletins",
    "One Country",
    "Pulse of the Pioneer",
    "Star of the West",
    "Teach! Canada",
    "Teaching Bulletin of the Nine Year Plan",
    "U.S. Supplement",
    "UK Bahá’í Review",
    "World Order",
    "World Unity"
]

st.set_page_config(page_title="Category Sweeper", page_icon="🧹", layout="wide")

# ==============================================================================
# 1. HELPER FUNCTIONS
# ==============================================================================

def int_to_roman(num):
    """Converts an integer to a lowercase roman numeral (1 -> i, 5 -> v)."""
    val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
    syb = ["m", "cm", "d", "cd", "c", "xc", "l", "xl", "x", "ix", "v", "iv", "i"]
    roman_num = ''
    i = 0
    num = abs(num)
    if num == 0: return "i" 

    while num > 0:
        for _ in range(num // val[i]):
            roman_num += syb[i]
            num -= val[i]
        i += 1
    return roman_num

def calculate_page_label(pdf_page_num, anchor_pdf_page):
    """
    Determines if a page should be Roman (i, ii) or Arabic (1, 2).
    anchor_pdf_page: The PDF page number that corresponds to Book Page 1.
    """
    if anchor_pdf_page is None:
        # No anchor found, assume PDF page 1 = Book page 1
        return str(pdf_page_num)
        
    if pdf_page_num < anchor_pdf_page:
        # Front matter (before Page 1) -> Roman Numerals based on PDF page
        return int_to_roman(pdf_page_num)
    else:
        # Main content -> Offset Calculation
        # Ex: PDF 10 is Page 1. So PDF 10 - 10 + 1 = 1.
        return str(pdf_page_num - anchor_pdf_page + 1)

def log_small(msg, color="black"):
    """Prints a compact log line."""
    st.markdown(f"<span style='font-size:13px; font-family:monospace; color:{color};'>{msg}</span>", unsafe_allow_html=True)
    
def find_anchor_offset(wikitext):
    """
    Scans for any {{page|N|...|page=X}} where N is an integer to establish the offset.
    Returns X (the PDF page number) corresponding to Book Page 1.
    """
    tags = re.finditer(r'\{\{page\|(.*?)\}\}', wikitext, re.IGNORECASE | re.DOTALL)
    
    for match in tags:
        params = match.group(1)
        # The first parameter is the page label (e.g., '42' in {{page|42|...}})
        label = params.split('|')[0].strip()
        
        if label.isdigit():
            # Find the internal PDF page number
            page_match = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
            if page_match:
                book_page = int(label)
                pdf_page = int(page_match.group(1))
                # Calculate Anchor: PDF Page corresponding to Book Page 1
                # Formula: Anchor = PDF_Page - Book_Page + 1
                return pdf_page - book_page + 1
    return None

def fetch_parent_author(subpage_title):
    """
    If title is 'Book/Text', fetches 'Book' and extracts the author from its header.
    Returns the author string or empty string if not found.
    """
    # 1. Determine Parent Title (strip /Text or other subpages)
    if "/" not in subpage_title:
        return ""
    
    parent_title = subpage_title.rsplit("/", 1)[0]
    
    # 2. Fetch Parent Text
    # We use the existing fetch_wikitext function imported from src.mediawiki_uploader
    parent_text, err = fetch_wikitext(parent_title)
    
    if err or not parent_text:
        return ""

    # 3. Extract Author
    # Looks for | author = Name (multiline safe)
    author_match = re.search(r'\|\s*author\s*=\s*([^\n|]+)', parent_text, re.IGNORECASE)
    
    if author_match:
        return author_match.group(1).strip()
    
    return ""

def get_processing_bounds(wikitext, total_pdf_pages, is_text_subpage):
    """
    Returns (start_page, end_page) for PDF processing.
    If /Text, returns (1, total_pdf_pages).
    Otherwise, scans for the first {{page|...|page=X}} to start,
    and counts total {{page}} tags to determine the end.
    """
    if is_text_subpage:
        return 1, total_pdf_pages

    tags = list(re.finditer(r'\{\{page\|(.*?)\}\}', wikitext, re.IGNORECASE | re.DOTALL))
    
    if not tags:
        # Fallback: If no tags found, process whole PDF
        return 1, total_pdf_pages
        
    # Parse the first tag to find the starting PDF page
    first_params = tags[0].group(1)
    page_match = re.search(r'page\s*=\s*(\d+)', first_params, re.IGNORECASE)
    
    if page_match:
        start_page = int(page_match.group(1))
        # End page is start + count - 1
        end_page = start_page + len(tags) - 1
        # Clamp to physical PDF limit just in case
        return start_page, min(end_page, total_pdf_pages)
        
    return 1, total_pdf_pages

def process_header(wikitext, wiki_title):
    """
    Updates existing header OR creates a new one.
    - Enforces {{ps|1}} in 'notes'.
    - Adds {{bnreturn}} ONLY if title ends in /Text.
    - Adds 'categories = YYYY' if creating new header and Year category found.
    - Fills 'author' from Parent Page if missing.
    """
    is_text_page = wiki_title.endswith("/Text")
    
    # --- Parent Author Lookup ---
    # We only look this up if we need it (lazy loading inside the logic below)
    parent_author = None 

    def get_author_fill():
        nonlocal parent_author
        if parent_author is None:
            parent_author = fetch_parent_author(wiki_title)
        return parent_author

    # 1. Check for Existing Header
    header_match = re.search(r'(\{\{header\s*\|.*?\n\}\})', wikitext, re.IGNORECASE | re.DOTALL)
    
    if header_match:
        old_header = header_match.group(1)
        new_header = old_header
        
        # --- Update Notes Section ---
        # (Same logic as before for ps|1 and bnreturn)
        if re.search(r'\|\s*notes\s*=', new_header):
            if "{{ps|" in new_header:
                new_header = re.sub(r'\{\{ps\|\d+\}\}', '{{ps|1}}', new_header)
            else:
                new_header = re.sub(r'(\|\s*notes\s*=\s*)(.*)', r'\1{{ps|1}}\2', new_header)
            
            if is_text_page and "{{bnreturn}}" not in new_header:
                new_header = re.sub(r'(\|\s*notes\s*=\s*)(.*)', r'\1\2{{bnreturn}}', new_header)
            if not is_text_page and "{{bnreturn}}" in new_header:
                new_header = new_header.replace("{{bnreturn}}", "")
        else:
            notes_val = "{{ps|1}}"
            if is_text_page: notes_val += "{{bnreturn}}"
            new_header = new_header.rstrip("}") + f"\n | notes       = {notes_val}\n}}"

        # --- Update Author Section ---
        # Check if author param exists but is empty OR doesn't exist
        author_param_match = re.search(r'\|\s*author\s*=\s*([^\n|]*)', new_header, re.IGNORECASE)
        
        if author_param_match:
            current_val = author_param_match.group(1).strip()
            if not current_val:
                # Param exists but is empty -> Fill it
                found_author = get_author_fill()
                if found_author:
                    # Replace "| author = " with "| author = Horace Holley"
                    new_header = re.sub(r'(\|\s*author\s*=\s*)', f"\\1{found_author}", new_header, count=1)
        else:
            # Param missing entirely -> Add it
            found_author = get_author_fill()
            if found_author:
                # Insert author param after title (or at start if title missing)
                if "| title" in new_header:
                    new_header = re.sub(r'(\|\s*title\s*=.*?\n)', f"\\1 | author      = {found_author}\n", new_header)
                else:
                    new_header = new_header.replace("{{header", f"{{header\n | author      = {found_author}")

        # Replace in text
        if new_header != old_header:
            return wikitext.replace(old_header, new_header)
        return wikitext

    else:
        # 2. Create New Header
        cat_match = re.search(r'\[\[Category:\s*(\d{4})', wikitext)
        cat_str = cat_match.group(1) if cat_match else ""

        notes_str = "{{ps|1}}"
        if is_text_page:
            notes_str += "{{bnreturn}}"

        # Try to find author for the new header
        found_author = get_author_fill()

        new_header = f"""{{{{header
 | title      = [[../]]
 | author     = {found_author}
 | translator = 
 | section    = 
 | previous   = 
 | next       = 
 | notes      = {notes_str}
 | categories = {cat_str}
}}}}"""
        
        return new_header + "\n" + wikitext.lstrip()

def find_and_fix_tag_by_page_num(wikitext, pdf_filename, pdf_page_num, correct_label):
    """
    Robustly finds {{page|...|file=...|page=X}} regardless of the existing label.
    Replaces the ENTIRE tag block (including any trailing {{ocr}}) with the correct clean tag.
    """
    # 1. Find all {{page}} tags in the text
    # Group 1: The full {{page}} tag
    # Group 2: The inner content of {{page}}
    # Group 3: The optional {{ocr}} tag following it
    tags = list(re.finditer(r'(\{\{page\|(.*?)\}\})(\s*\{\{ocr\}\})?', wikitext, re.IGNORECASE | re.DOTALL))
    
    for match in tags:
        full_tag_block = match.group(0) # Includes {{ocr}} if present
        params = match.group(2)         # Content inside {{page|...}}
        
        # Check if this tag belongs to our file and page
        file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
        page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
        
        if file_check and page_check:
            found_filename = file_check.group(1).strip()
            
            # Simple filename match (ignore case/paths)
            if int(page_check.group(1)) == pdf_page_num and \
               os.path.basename(found_filename).lower() == os.path.basename(pdf_filename).lower():
                
                # Found it. Create clean new tag with correct label.
                # Note: We do NOT append {{ocr}}. 
                # This effectively deletes {{ocr}} from the page if it was in 'full_tag_block'.
                new_tag = f"{{{{page|{correct_label}|file={pdf_filename}|page={pdf_page_num}}}}}"
                
                # Replace the entire old block with the new tag
                wikitext = wikitext.replace(full_tag_block, new_tag)
                
                st.toast(f"Fixed Tag: PDF {pdf_page_num} -> {correct_label}", icon="🔧")
                return wikitext
                
    return wikitext

def build_pdf_index(root_folder):
    """
    Recursively scans the folder and creates a dictionary:
    { "filename.pdf": "/full/path/to/filename.pdf" }
    """
    pdf_index = {}
    st.toast(f"Indexing PDFs in {root_folder}...", icon="🔍")
    
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower().endswith(".pdf"):
                # We store just the filename as key
                pdf_index[f] = os.path.join(dirpath, f)
                
    return pdf_index

def get_category_members(category_name, limit=5000):
    """
    Fetches pages belonging to a specific category.
    Handles pagination to get all members.
    """
    members = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category_name,
        "cmlimit": "500", # Max for non-bots, 5000 for bots
        "format": "json"
    }
    
    headers = {"User-Agent": "BahaiWorksSweeper/1.0"}
    
    while True:
        try:
            response = requests.get(API_URL, params=params, headers=headers, timeout=30)
            data = response.json()
            
            if 'error' in data:
                st.error(f"API Error: {data['error']}")
                break
                
            chunk = data.get('query', {}).get('categorymembers', [])
            members.extend(chunk)
            
            # Check for continuation
            if 'continue' in data:
                params.update(data['continue'])
            else:
                break
                
            if len(members) >= limit:
                break
                
        except Exception as e:
            st.error(f"Network Error fetching category: {e}")
            break
            
    return members

def get_page_image_data(pdf_path, page_num_1_based):
    """Extracts image from local PDF using PyMuPDF."""
    try:
        doc = fitz.open(pdf_path)
        if page_num_1_based > len(doc) or page_num_1_based < 1:
            doc.close()
            return None
        
        page = doc.load_page(page_num_1_based - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # High res for OCR
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        doc.close()
        return img
    except Exception as e:
        st.error(f"Error reading PDF {pdf_path}: {e}")
        return None

# ==============================================================================
# 2. STATE MANAGEMENT
# ==============================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                data = json.load(f)
                if "member_index" in data and "pdf_page_num" in data:
                    return data
            except json.JSONDecodeError:
                pass
    return {"member_index": 0, "pdf_page_num": 1, "last_title": None}

def save_state(member_index, pdf_page_num, title):
    state = {
        "member_index": member_index, 
        "pdf_page_num": pdf_page_num,
        "last_title": title
    }
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def reset_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    return {"member_index": 0, "pdf_page_num": 1, "last_title": None}

# ==============================================================================
# 3. UI & LOGIC
# ==============================================================================

st.title("🧹 Category Sweeper: Full PDF Processing")
st.markdown("Autonomously iterates through the maintenance category, finds local PDFs, updates headers, fixes tags, and proofreads.")

# --- Sidebar ---
st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio(
    "OCR Strategy", 
    ["Gemini (Default)", "DocAI Only"], 
    help="DocAI Only skips Gemini and goes straight to Google Cloud Vision OCR."
)

run_mode = st.sidebar.radio("Run Mode", ["Test (1 Book Only)", "Production (Continuous)"])

st.sidebar.divider()
state = load_state()

if st.sidebar.button("🗑️ Reset State"):
    state = reset_state()
    st.sidebar.success("State cleared. Will start from top of category.")
    st.rerun()

st.sidebar.info(f"Resuming at Index: {state['member_index']}\nPDF Page: {state['pdf_page_num']}")

# --- Main Work Area ---

if not os.path.exists(input_folder):
    st.error(f"❌ Input folder does not exist: {input_folder}")
    st.stop()

# Layout
col1, col2 = st.columns([1, 1])
with col1:
    start_btn = st.button("🚀 Start Sweeper", type="primary", use_container_width=True)
with col2:
    stop_btn = st.button("🛑 Stop After Current Page", use_container_width=True)

status_container = st.container(border=True)
log_area = st.empty()

if start_btn:
    # --- UI Setup ---
    progress_bar = st.progress(0)
    current_status_line = st.empty() # For transient updates (like exclusions)
    
    # 1. Index PDFs
    with st.spinner("Indexing Local PDFs..."):
        pdf_index = build_pdf_index(input_folder)
        st.success(f"Indexed {len(pdf_index)} PDF files.")
        
    # 2. Fetch Category
    with st.spinner("Fetching Wiki Category Members..."):
        members = get_category_members("Category:Pages_needing_proofreading")
        st.info(f"Found {len(members)} pages needing proofreading.")

    # 3. Resume Logic
    start_idx = state['member_index']
    
    # --- OUTER LOOP: Wiki Pages ---
    for i in range(start_idx, len(members)):
        
        page_obj = members[i]
        wiki_title = page_obj['title']
        
        # --- Check Exclusions ---
        if any(wiki_title.startswith(exclude) for exclude in EXCLUDED_TITLES):
            # Transient update (overwrites itself) to prevent spamming the log
            current_status_line.text(f"Scanning {i}/{len(members)}: Skipping excluded '{wiki_title}'...")
            save_state(i + 1, 1, wiki_title)
            continue
        
        # --- Log Processing Start ---
        log_small(f"📚 ({i+1}/{len(members)}) Processing: <b>{wiki_title}</b>", color="#444")

        # A. Fetch Wikitext
        current_text, err = fetch_wikitext(wiki_title)
        if err:
            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Error fetching text: {err}", color="red")
            continue

        # B. Header Processing
        current_text = process_header(current_text, wiki_title)

        # C. Identify PDF Filename
        file_match = re.search(r'file\s*=\s*([^|}\n]+)', current_text, re.IGNORECASE)
        if not file_match:
            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;⚠️ 'file=' parameter not found in wikitext. Skipping.", color="#d97706") # Orange
            save_state(i + 1, 1, wiki_title) 
            continue
            
        pdf_filename = file_match.group(1).strip()
        
        # D. Locate Local PDF
        local_path = pdf_index.get(pdf_filename)
        if not local_path:
            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ PDF '{pdf_filename}' not found in local index. Skipping.", color="red")
            save_state(i + 1, 1, wiki_title)
            continue
            
        # E. Determine Offset (Anchor)
        anchor_pdf_page = find_anchor_offset(current_text)
            
        # F. Determine Start Page for PDF Loop
        try:
            doc = fitz.open(local_path)
            total_pdf_pages = len(doc)
            doc.close()
        except Exception as e:
            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Failed to open local PDF: {e}", color="red")
            continue

        # G. Processing Bounds
        is_text_subpage = wiki_title.endswith("/Text")
        scope_start, scope_end = get_processing_bounds(current_text, total_pdf_pages, is_text_subpage)

        if i == state['member_index']:
            start_pdf_page = max(state['pdf_page_num'], scope_start)
        else:
            start_pdf_page = scope_start
            
        # Reset Gemini failure counter for this book
        gemini_failures = 0

        # --- INNER LOOP: Pages ---
        # --- Processing State ---
        gemini_consecutive_failures = 0
        docai_cooldown_pages = 0
        permanent_docai = False

        # --- INNER LOOP: Pages ---
        for pdf_page in range(start_pdf_page, scope_end + 1):
            
            # Stop Check
            if stop_btn:
                st.warning("Stopping requested...")
                break 
            
            correct_label = calculate_page_label(pdf_page, anchor_pdf_page)
            # Transient status update
            current_status_line.text(f"Working on: {wiki_title} | Page {correct_label}")

            # 1. Get Image
            img = get_page_image_data(local_path, pdf_page)
            if not img:
                log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Image Error (Page {correct_label})", color="red")
                continue

            # 2. TAG FIXING
            if pdf_page > start_pdf_page:
                current_text, _ = fetch_wikitext(wiki_title)
            
            current_text = find_and_fix_tag_by_page_num(current_text, pdf_filename, pdf_page, correct_label)

            # 3. AI Processing
            final_text = ""
            try:
                # --- Determine Strategy ---
                # We force DocAI if:
                # 1. User selected "DocAI Only"
                # 2. We are permanently locked out of Gemini (3rd strike)
                # 3. We are in a "cooldown" period (2 failures triggered 5 pages of DocAI)
                force_docai = (ocr_strategy == "DocAI Only") or permanent_docai or (docai_cooldown_pages > 0)

                if force_docai:
                    # --- DocAI Path ---
                    raw_ocr = transcribe_with_document_ai(img)
                    if not raw_ocr or "ERROR" in raw_ocr:
                        # Fallback for DocAI failure (rare)
                        log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;⚠️ DocAI failed on {correct_label}. Trying Gemini fallback.", color="#d97706")
                        final_text = proofread_with_formatting(img)
                    else:
                        final_text = reformat_raw_text(raw_ocr)
                    
                    # Decrement Cooldown if active
                    if docai_cooldown_pages > 0:
                        docai_cooldown_pages -= 1
                        if docai_cooldown_pages == 0:
                            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;🟢 Cooldown complete. Re-enabling Gemini next page.", color="green")

                else:
                    # --- Gemini Path ---
                    final_text = proofread_with_formatting(img)
                    
                    if final_text and "GEMINI_ERROR" in final_text:
                        gemini_consecutive_failures += 1
                        
                        # Handle Failure Logic
                        if gemini_consecutive_failures == 2:
                            docai_cooldown_pages = 5
                            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;⚠️ 2 Consecutive Failures on {correct_label}. Switching to DocAI for 5 pages.", color="#d97706")
                        
                        elif gemini_consecutive_failures >= 3:
                            permanent_docai = True
                            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;⛔ 3rd Strike (Retry Failed) on {correct_label}. Switching to DocAI for remainder of book.", color="red")
                        
                        else:
                            log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;⚠️ Gemini Error ({gemini_consecutive_failures}/2) on {correct_label}. Fallback to DocAI.", color="#d97706")

                        # Immediate Fallback for THIS page
                        raw_ocr = transcribe_with_document_ai(img)
                        final_text = reformat_raw_text(raw_ocr)
                    
                    else:
                        # Success! Reset consecutive counter.
                        gemini_consecutive_failures = 0

                if not final_text or "ERROR" in final_text:
                    log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Processing failed for Page {correct_label}", color="red")
                    continue

                # 4. Inject & Upload
                new_wikitext, inject_err = inject_text_into_page(current_text, correct_label, final_text, pdf_filename)
                
                if inject_err:
                    log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Injection Error ({correct_label}): {inject_err}", color="red")
                    continue
                
                # 5. Check for __NOTOC__
                if pdf_page == scope_end and "__NOTOC__" not in new_wikitext:
                    new_wikitext += "\n__NOTOC__"
                
                # Upload
                res = upload_to_bahaiworks(wiki_title, new_wikitext, f"Bot: Proofread {correct_label} (PDF {pdf_page})")
                
                if res.get('edit', {}).get('result') == 'Success':
                    save_state(i, pdf_page + 1, wiki_title)
                    current_text = new_wikitext 
                else:
                    log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Upload API Error ({correct_label}): {res}", color="red")
                    break 
            
            except Exception as e:
                log_small(f"&nbsp;&nbsp;&nbsp;&nbsp;❌ Exception ({correct_label}): {e}", color="red")
                break 
                
            time.sleep(1)

        if stop_btn:
            break 

        # Book Completed
        if pdf_page == scope_end:
            save_state(i + 1, 1, wiki_title) 

        # Update UI
        progress_bar.progress((i + 1 - start_idx) / (len(members) - start_idx))
        
        if run_mode.startswith("Test"):
            st.info("Test Mode: Stopping after 1 book.")
            break
            
    st.success(f"Sweep Complete!")
