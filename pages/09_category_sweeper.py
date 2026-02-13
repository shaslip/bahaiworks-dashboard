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

def find_page_one_anchor(wikitext):
    """
    Scans for {{page|1|file=...|page=X}} to establish the offset.
    Returns X (the PDF page number) corresponding to Book Page 1.
    """
    match = re.search(r'\{\{page\|\s*1\s*\|[^}]*?page\s*=\s*(\d+)', wikitext, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def process_header(wikitext, wiki_title):
    """
    Updates existing header OR creates a new one.
    - Enforces {{ps|1}} in 'notes'.
    - Adds {{bnreturn}} ONLY if title ends in /Text.
    - Adds 'categories = YYYY' if creating new header and Year category found.
    """
    is_text_page = wiki_title.endswith("/Text")
    
    # 1. Check for Existing Header
    header_match = re.search(r'(\{\{header\s*\|.*?\n\}\})', wikitext, re.IGNORECASE | re.DOTALL)
    
    if header_match:
        old_header = header_match.group(1)
        new_header = old_header
        
        # --- Update Notes Section ---
        # Check if notes param exists
        if re.search(r'\|\s*notes\s*=', new_header):
            # Enforce ps|1
            if "{{ps|" in new_header:
                new_header = re.sub(r'\{\{ps\|\d+\}\}', '{{ps|1}}', new_header)
            else:
                # Append ps|1 to existing notes
                new_header = re.sub(r'(\|\s*notes\s*=\s*)(.*)', r'\1{{ps|1}}\2', new_header)
            
            # Enforce bnreturn (Add if missing and needed)
            if is_text_page and "{{bnreturn}}" not in new_header:
                new_header = re.sub(r'(\|\s*notes\s*=\s*)(.*)', r'\1\2{{bnreturn}}', new_header)
            
            # Remove bnreturn if NOT needed
            if not is_text_page and "{{bnreturn}}" in new_header:
                new_header = new_header.replace("{{bnreturn}}", "")
                
        else:
            # Create notes param
            notes_val = "{{ps|1}}"
            if is_text_page: notes_val += "{{bnreturn}}"
            # Insert before the closing brackets
            new_header = new_header.rstrip("}") + f"\n | notes      = {notes_val}\n}}"

        # Replace in text
        if new_header != old_header:
            return wikitext.replace(old_header, new_header)
        return wikitext

    else:
        # 2. Create New Header
        # Find Year for Category
        cat_match = re.search(r'\[\[Category:\s*(\d{4})', wikitext)
        cat_str = cat_match.group(1) if cat_match else ""

        notes_str = "{{ps|1}}"
        if is_text_page:
            notes_str += "{{bnreturn}}"

        new_header = f"""{{{{header
 | title      = [[../]]
 | author     = 
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
    Replaces the ENTIRE tag with {{page|CORRECT_LABEL|file=...|page=X}}.
    Also nukes {{ocr}} if it hangs off the end.
    """
    # 1. Find all {{page}} tags in the text
    # We iterate to find the specific one matching our PDF page number
    tags = list(re.finditer(r'(\{\{page\|(.*?)\}\})(\s*\{\{ocr\}\})?', wikitext, re.IGNORECASE | re.DOTALL))
    
    for match in tags:
        full_tag_block = match.group(0) # Includes {{ocr}} if present
        params = match.group(2)         # Content inside {{page|...}}
        
        # Check if this tag belongs to our file and page
        file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
        page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
        
        if file_check and page_check:
            found_file = file_check.group(1).strip()
            found_page = int(page_check.group(1))
            
            # Simple filename match (ignore case/paths)
            if found_page == pdf_page_num and os.path.basename(found_file).lower() == os.path.basename(pdf_filename).lower():
                
                # FOUND IT!
                # Construct clean new tag
                new_tag = f"{{{{page|{correct_label}|file={pdf_filename}|page={pdf_page_num}}}}}"
                
                # Replace the entire old block (including {{ocr}}) with the new tag
                # Note: We replace only this specific instance string
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
    progress_bar = st.progress(0)
    
    # --- OUTER LOOP: Wiki Pages ---
    for i in range(start_idx, len(members)):
        
        page_obj = members[i]
        wiki_title = page_obj['title']
        
        status_container.markdown(f"### 📚 Processing Book ({i+1}/{len(members)}): `{wiki_title}`")
        
        # A. Fetch Wikitext
        log_area.text(f"Fetching source for {wiki_title}...")
        current_text, err = fetch_wikitext(wiki_title)
        
        if err:
            log_area.text(f"❌ Error fetching text: {err}. Skipping.")
            continue

        # B. Header Processing
        # Ensure header is correct (ps|1, categories, etc.) before we start
        current_text = process_header(current_text, wiki_title)

        # C. Identify PDF Filename from Wikitext
        # We search for ANY instance of file=... to find the source PDF
        file_match = re.search(r'file\s*=\s*([^|}\n]+)', current_text, re.IGNORECASE)
        if not file_match:
            st.warning(f"⚠️ Could not find 'file=' in {wiki_title}. Skipping book.")
            save_state(i + 1, 1, wiki_title) 
            continue
            
        pdf_filename = file_match.group(1).strip()
        
        # D. Locate Local PDF
        local_path = pdf_index.get(pdf_filename)
        if not local_path:
            st.error(f"⚠️ PDF '{pdf_filename}' not found in local index. Skipping.")
            save_state(i + 1, 1, wiki_title)
            continue
            
        # E. Determine Offset (Anchor)
        anchor_pdf_page = find_page_one_anchor(current_text)
        if anchor_pdf_page:
            log_area.text(f"⚓ Anchor Found: Physical Page 1 is PDF Page {anchor_pdf_page}")
        else:
            log_area.text(f"⚠️ No Anchor found. Assuming PDF Page 1 = Book Page 1.")
            
        # F. Determine Start Page for PDF Loop
        if i == state['member_index']:
            start_pdf_page = state['pdf_page_num']
        else:
            start_pdf_page = 1

        # --- INNER LOOP: PDF Pages ---
        try:
            doc = fitz.open(local_path)
            total_pdf_pages = len(doc)
            doc.close()
        except Exception as e:
            st.error(f"Failed to open PDF: {e}")
            continue

        for pdf_page in range(start_pdf_page, total_pdf_pages + 1):
            
            # Stop Check
            if stop_btn:
                st.warning("Stopping requested...")
                break # Breaks inner loop
            
            # 1. Calculate Label
            correct_label = calculate_page_label(pdf_page, anchor_pdf_page)
            
            log_area.text(f"📸 Processing PDF Page {pdf_page}/{total_pdf_pages} (Label: {correct_label})...")

            # 2. Get Image
            img = get_page_image_data(local_path, pdf_page)
            if not img:
                log_area.text(f"❌ Failed to extract image from PDF. Skipping page.")
                continue

            # 3. TAG FIXING (The Critical Fix)
            # We fix the tag in memory BEFORE proofreading. 
            # This ensures inject_text_into_page finds a tag labeled 'i' instead of '-2'.
            
            # Fetch fresh text for pages 2+ to catch up with server state
            if pdf_page > start_pdf_page:
                current_text, _ = fetch_wikitext(wiki_title)
            
            # Find {{page|...|page=1}} and force label to 'i' (or whatever correct_label is)
            current_text = find_and_fix_tag_by_page_num(current_text, pdf_filename, pdf_page, correct_label)

            # 4. AI Processing (Gemini -> DocAI Fallback)
            final_text = ""
            try:
                if ocr_strategy == "Gemini (Default)":
                    log_area.text("✨ Asking Gemini to proofread...")
                    final_text = proofread_with_formatting(img)
                    
                    if "GEMINI_ERROR" in final_text:
                        log_area.text("⚠️ Gemini Error/Copyright. Falling back to DocAI...")
                        raw_ocr = transcribe_with_document_ai(img)
                        final_text = reformat_raw_text(raw_ocr)
                else:
                    log_area.text("🤖 DocAI Direct Mode...")
                    raw_ocr = transcribe_with_document_ai(img)
                    final_text = reformat_raw_text(raw_ocr)
                    
                if not final_text or "ERROR" in final_text:
                    st.error(f"Processing failed for PDF {pdf_page}: {final_text}")
                    continue
                    
                # 5. Inject & Upload
                log_area.text("💾 Uploading to Wiki...")
                
                # inject_text_into_page will replace content if tag exists, or append if it doesn't.
                # Since we normalized the tag in step 3, it should find it correctly now.
                new_wikitext, inject_err = inject_text_into_page(current_text, correct_label, final_text, pdf_filename)
                
                if inject_err:
                    st.error(f"Injection Error: {inject_err}")
                    continue
                
                # 6. Check for __NOTOC__ (Last Page Only)
                if pdf_page == total_pdf_pages:
                    if "__NOTOC__" not in new_wikitext:
                        new_wikitext += "\n__NOTOC__"
                        log_area.text("📝 Appending __NOTOC__.")
                
                # Upload
                res = upload_to_bahaiworks(wiki_title, new_wikitext, f"Bot: Proofread {correct_label} (PDF {pdf_page})")
                
                if res.get('edit', {}).get('result') == 'Success':
                    st.success(f"✅ Completed: {correct_label}")
                    save_state(i, pdf_page + 1, wiki_title)
                    current_text = new_wikitext # Sync memory
                else:
                    st.error(f"Upload API Failed: {res}")
                    break # Stop inner loop
            
            except Exception as e:
                st.error(f"Exception processing PDF {pdf_page}: {e}")
                break # Stop inner loop
                
            time.sleep(1) # Polite API pause

        # Check breaks
        if stop_btn:
            break 

        # Book Completed
        if pdf_page == total_pdf_pages:
            st.success(f"✅ Completed Book: {wiki_title}")
            save_state(i + 1, 1, wiki_title) 

        # Update UI
        progress_bar.progress((i + 1 - start_idx) / (len(members) - start_idx))
        
        # Test Mode Check
        if run_mode.startswith("Test"):
            st.info("Test Mode: Stopping after 1 book.")
            break
            
    st.success(f"Sweep Complete!")
