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
    """
    if anchor_pdf_page is None:
        return str(pdf_page_num)
        
    if pdf_page_num < anchor_pdf_page:
        return int_to_roman(pdf_page_num)
    else:
        return str(pdf_page_num - anchor_pdf_page + 1)

def find_page_one_anchor(wikitext):
    """Scans for {{page|1|file=...|page=X}} to establish the offset."""
    match = re.search(r'\{\{page\|\s*1\s*\|[^}]*?page\s*=\s*(\d+)', wikitext, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def build_pdf_index(root_folder):
    """Indexes PDFs: { 'file.pdf': '/path/to/file.pdf' }"""
    pdf_index = {}
    st.toast(f"Indexing PDFs in {root_folder}...", icon="🔍")
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower().endswith(".pdf"):
                pdf_index[f] = os.path.join(dirpath, f)
    return pdf_index

def get_category_members(category_name, limit=5000):
    """Fetches pages from the maintenance category."""
    members = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category_name,
        "cmlimit": "500",
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
            
            if 'continue' in data:
                params.update(data['continue'])
            else:
                break
            if len(members) >= limit:
                break
        except Exception as e:
            st.error(f"Network Error: {e}")
            break
            
    return members

def get_page_image_data(pdf_path, page_num_1_based):
    try:
        doc = fitz.open(pdf_path)
        if page_num_1_based > len(doc) or page_num_1_based < 1:
            doc.close()
            return None
        page = doc.load_page(page_num_1_based - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        doc.close()
        return img
    except Exception as e:
        st.error(f"Error reading PDF {pdf_path}: {e}")
        return None

def process_header(wikitext, wiki_title):
    """Updates existing header OR creates a new one with correct categories/notes."""
    is_text_page = wiki_title.endswith("/Text")
    
    # 1. Check for Existing Header
    header_match = re.search(r'(\{\{header\s*\|.*?\n\}\})', wikitext, re.IGNORECASE | re.DOTALL)
    
    if header_match:
        old_header = header_match.group(1)
        new_header = old_header
        
        # --- Update Notes Section ---
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
            new_header = new_header.rstrip("}") + f"\n | notes      = {notes_val}\n}}"

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

def normalize_tag_label(wikitext, pdf_filename, pdf_page_num, correct_label):
    """
    Finds the existing tag for a specific PDF page (even if labeled '-2') 
    and renames the label to the correct one (e.g., 'i').
    Returns the updated wikitext.
    """
    # Regex to find: {{page| <ANY_LABEL> | file=FILENAME | page=PDF_NUM }}
    # We use re.escape for filename to handle dots/spaces safe
    pattern = re.compile(
        r'(\{\{page\s*\|\s*)([^|]+?)(\s*\|\s*file\s*=\s*' + re.escape(pdf_filename) + 
        r'\s*\|\s*page\s*=\s*' + str(pdf_page_num) + r'\s*(?:\||\}\}))', 
        re.IGNORECASE
    )
    
    match = pattern.search(wikitext)
    
    if match:
        current_label = match.group(2).strip()
        if current_label != correct_label:
            st.toast(f"Normalizing Label: {current_label} -> {correct_label}", icon="🔧")
            # Reconstruct the start of the tag with the CORRECT label
            new_start = match.group(1) + correct_label + match.group(3)
            # Replace only this specific occurrence
            wikitext = wikitext.replace(match.group(0), new_start)
            
    return wikitext

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
st.markdown("Iterates category members, normalizes tags, updates headers, proofreads **every page**, and adds `__NOTOC__`.")

# --- Sidebar ---
st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio("OCR Strategy", ["Gemini (Default)", "DocAI Only"])
run_mode = st.sidebar.radio("Run Mode", ["Test (1 Book Only)", "Production (Continuous)"])

st.sidebar.divider()
state = load_state()

if st.sidebar.button("🗑️ Reset State"):
    state = reset_state()
    st.sidebar.success("State cleared.")
    st.rerun()

st.sidebar.info(f"Resuming at Index: {state['member_index']}\nPDF Page: {state['pdf_page_num']}")

# --- Main Work Area ---

if not os.path.exists(input_folder):
    st.error(f"❌ Input folder does not exist: {input_folder}")
    st.stop()

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
    with st.spinner("Fetching Category Members..."):
        members = get_category_members("Category:Pages_needing_proofreading")
        st.info(f"Found {len(members)} wiki pages needing attention.")

    # 3. Resume Logic
    start_idx = state['member_index']
    progress_bar = st.progress(0)
    
    # --- OUTER LOOP: Wiki Pages ---
    for i in range(start_idx, len(members)):
        
        page_obj = members[i]
        wiki_title = page_obj['title']
        
        status_container.markdown(f"### 📚 Processing Book ({i+1}/{len(members)}): `{wiki_title}`")

        # A. Fetch Text 
        current_text, err = fetch_wikitext(wiki_title)
        if err:
            st.error(f"❌ Error fetching {wiki_title}: {err}")
            continue

        # B. Header Processing
        current_text = process_header(current_text, wiki_title)

        # C. Identify PDF Filename
        file_match = re.search(r'file\s*=\s*([^|}\n]+)', current_text, re.IGNORECASE)
        if not file_match:
            st.warning(f"⚠️ Could not find 'file=' in {wiki_title}. Skipping book.")
            save_state(i + 1, 1, wiki_title) 
            continue
            
        pdf_filename = file_match.group(1).strip()
        
        # D. Locate Local PDF
        local_path = pdf_index.get(pdf_filename)
        if not local_path:
            st.error(f"❌ PDF '{pdf_filename}' not found locally. Skipping book.")
            save_state(i + 1, 1, wiki_title)
            continue
            
        # E. Determine Offset (Anchor)
        anchor_pdf_page = find_page_one_anchor(current_text)
        if anchor_pdf_page:
            log_area.text(f"⚓ Anchor Found: Physical Page 1 is PDF Page {anchor_pdf_page}")
        else:
            log_area.text(f"⚠️ No Anchor found. Assuming PDF Page 1 = Book Page 1.")

        # F. Determine Start Page
        if i == state['member_index']:
            start_pdf_page = state['pdf_page_num']
        else:
            start_pdf_page = 1

        # --- INNER LOOP: PDF Pages ---
        try:
            doc = fitz.open(local_path)
            total_pdf_pages = len(doc)
            doc.close()
        except:
            st.error("Failed to open PDF.")
            continue

        for pdf_page in range(start_pdf_page, total_pdf_pages + 1):
            
            if stop_btn:
                st.warning("🛑 Stop requested...")
                break 

            # 1. Calculate Label
            correct_label = calculate_page_label(pdf_page, anchor_pdf_page)
            
            log_area.text(f"🔨 Processing {wiki_title} -> PDF {pdf_page}/{total_pdf_pages} (Label: {correct_label})")

            # 2. Get Image
            img = get_page_image_data(local_path, pdf_page)
            if not img:
                st.error("Image extraction failed.")
                continue

            # 3. NORMALIZE TAG IN MEMORY
            # This is crucial: Convert {{page|-2...}} to {{page|i...}} so injection works
            # We must do this BEFORE fetching fresh text or injecting
            
            # Fetch fresh text for pages 2+ to catch up with server state
            if pdf_page > start_pdf_page:
                current_text, _ = fetch_wikitext(wiki_title)
                # Re-apply header fix if it wasn't saved yet? 
                # Actually, if page 1 saved, header is saved. 
                # If page 1 failed, we are retrying page 1, so header logic at step B covers it.
            
            # Normalize the specific tag we are about to fill
            current_text = normalize_tag_label(current_text, pdf_filename, pdf_page, correct_label)

            # 4. OCR / Proofread
            try:
                final_text = ""
                if ocr_strategy == "Gemini (Default)":
                    final_text = proofread_with_formatting(img)
                    if "GEMINI_ERROR" in final_text:
                        log_area.text("⚠️ Gemini Rejected. Fallback to DocAI.")
                        raw_ocr = transcribe_with_document_ai(img)
                        final_text = reformat_raw_text(raw_ocr)
                else:
                    raw_ocr = transcribe_with_document_ai(img)
                    final_text = reformat_raw_text(raw_ocr)
                
                if not final_text or "ERROR" in final_text:
                    st.error(f"Processing failed: {final_text}")
                    continue

                # 5. Inject Text
                new_wikitext, inject_err = inject_text_into_page(current_text, correct_label, final_text, pdf_filename)
                
                if inject_err:
                    st.error(f"Injection Error: {inject_err}")
                    continue
                
                # 6. Check for __NOTOC__ (Last Page Only)
                if pdf_page == total_pdf_pages:
                    if "__NOTOC__" not in new_wikitext:
                        new_wikitext += "\n__NOTOC__"
                        log_area.text("📝 Appending __NOTOC__.")

                # 7. Upload
                res = upload_to_bahaiworks(wiki_title, new_wikitext, f"Bot: Proofread {correct_label} (PDF {pdf_page})")
                
                if res.get('edit', {}).get('result') == 'Success':
                    st.toast(f"Saved {correct_label}", icon="✅")
                    save_state(i, pdf_page + 1, wiki_title)
                    current_text = new_wikitext # Sync memory
                else:
                    st.error(f"Upload Failed: {res}")
                    break 

            except Exception as e:
                st.error(f"Page Exception: {e}")
                break 
            
            time.sleep(1)

        if stop_btn:
            break 

        if pdf_page == total_pdf_pages:
            st.success(f"✅ Completed Book: {wiki_title}")
            save_state(i + 1, 1, wiki_title) 

        progress_bar.progress((i + 1 - start_idx) / (len(members) - start_idx))
        
        if run_mode.startswith("Test"):
            st.info("Test Mode: Stopping after 1 book.")
            break

    st.success("Sweep Complete!")
