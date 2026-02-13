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
    """Converts an integer to a lowercase roman numeral (i, ii, iii)."""
    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syb = [
        "m", "cm", "d", "cd",
        "c", "xc", "l", "xl",
        "x", "ix", "v", "iv",
        "i"
    ]
    roman_num = ''
    i = 0
    # Handle absolute value if negative numbers passed
    num = abs(num)
    if num == 0: return "i" # Fallback

    while  num > 0:
        for _ in range(num // val[i]):
            roman_num += syb[i]
            num -= val[i]
        i += 1
    return roman_num

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
                pdf_index[f] = os.path.join(dirpath, f)
                
    return pdf_index

def get_category_members(category_name, limit=5000):
    """Fetches pages belonging to a specific category."""
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
            st.error(f"Network Error fetching category: {e}")
            break
            
    return members

def find_page_one_offset(wikitext):
    """
    Scans the ENTIRE text to find the 'Anchor': {{page|1|file=...|page=X}}.
    Returns X (the PDF page number corresponding to Book Page 1).
    If not found, returns None.
    """
    # Look for {{page|1|...}} explicitly
    match = re.search(r'\{\{page\|\s*1\s*\|[^}]*?page\s*=\s*(\d+)', wikitext, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def find_next_unprocessed_tag(wikitext, offset_start_pdf_page):
    """
    Finds the first {{page}} tag that needs processing (marked by {{ocr}} or similar).
    Calculates the CORRECT label (Roman vs Arabic) based on the offset.
    
    Returns: (pdf_filename, pdf_page_num, correct_label_string, raw_tag_match)
    """
    # Regex finds {{page|...}} immediately followed by {{ocr}}
    # You can expand this regex if your placeholder isn't always {{ocr}}
    # matching: {{page|...}} ... {{ocr}} 
    # Note: This regex assumes the placeholder is fairly close to the tag
    
    # Simple strategy: Find all page tags, check if they are followed by {{ocr}}
    pattern = re.compile(r'(\{\{page\|(.*?)\}\})(\s*\{\{ocr\}\})', re.IGNORECASE | re.DOTALL)
    
    match = pattern.search(wikitext)
    
    if not match:
        return None, None, None, None

    full_tag_str = match.group(1)
    params_str = match.group(2)
    
    # Extract file and PDF page
    file_match = re.search(r'file\s*=\s*([^|]+)', params_str)
    page_match = re.search(r'page\s*=\s*(\d+)', params_str)
    
    if not file_match or not page_match:
        return None, None, None, None

    pdf_filename = file_match.group(1).strip()
    pdf_page_num = int(page_match.group(1))

    # --- CALCULATE CORRECT PHYSICAL LABEL ---
    if offset_start_pdf_page is None:
        # We don't have an anchor. Assuming sequential or existing is correct.
        # Fallback: Parse the current label from params
        label_match = params_str.split('|')[0].strip()
        correct_label = label_match
    else:
        # LOGIC:
        # If PDF page < Offset, use Roman.
        # If PDF page >= Offset, use (PDF - Offset + 1).
        
        if pdf_page_num < offset_start_pdf_page:
            # Front matter
            # Convert PDF page directly to roman? (Assuming PDF 1 = i)
            correct_label = int_to_roman(pdf_page_num)
        else:
            # Body content
            correct_label = str(pdf_page_num - offset_start_pdf_page + 1)

    return pdf_filename, pdf_page_num, correct_label, match.group(1) # Return original tag text for replacement

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
            return json.load(f)
    return {"last_processed_title": None, "status": "idle"}

def save_state(title):
    state = {"last_processed_title": title, "status": "running"}
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def reset_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    return {"last_processed_title": None, "status": "idle"}

# ==============================================================================
# 3. UI & LOGIC
# ==============================================================================

st.title("🧹 Category Sweeper: `Pages_needing_proofreading`")
st.markdown("Autonomously iterates through the maintenance category, calculates page offsets, and proofreads.")

# --- Sidebar ---
st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio(
    "OCR Strategy", 
    ["Gemini (Default)", "DocAI Only"], 
    help="DocAI Only skips Gemini and goes straight to Google Cloud Vision OCR."
)

run_mode = st.sidebar.radio("Run Mode", ["Test (1 Page Only)", "Production (Continuous)"])

st.sidebar.divider()
state = load_state()

if st.sidebar.button("🗑️ Reset State"):
    state = reset_state()
    st.sidebar.success("State cleared. Will start from top of category.")
    st.rerun()

st.sidebar.info(f"Last Processed: `{state['last_processed_title']}`")

# --- Main Work Area ---

if not os.path.exists(input_folder):
    st.error(f"❌ Input folder does not exist: {input_folder}")
    st.stop()

# Layout
col1, col2 = st.columns([1, 1])
with col1:
    start_btn = st.button("🚀 Start Sweeper", type="primary", use_container_width=True)
with col2:
    stop_btn = st.button("🛑 Stop After Current", use_container_width=True)

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

    # 3. Determine Start Point
    start_index = 0
    if state['last_processed_title']:
        for i, m in enumerate(members):
            if m['title'] == state['last_processed_title']:
                start_index = i # Retry the last one? or i+1? Let's stay on current if it wasn't finished, or move next.
                # Actually usually we want to move to next if success, but if we have multiple tags per page, we might stay.
                # For simplicity, let's assume one run clears the page from category or we move on.
                start_index = i 
                break
    
    # 4. Processing Loop
    processed_count = 0
    end_index = len(members)
    progress_bar = st.progress(0)
    
    for i in range(start_index, end_index):
        if stop_btn:
            st.warning("Stopping requested...")
            break
            
        page_obj = members[i]
        wiki_title = page_obj['title']
        
        status_container.markdown(f"### 🔨 Processing ({i+1}/{len(members)}): `{wiki_title}`")
        
        # A. Fetch Wikitext
        log_area.text(f"Fetching source for {wiki_title}...")
        current_text, err = fetch_wikitext(wiki_title)
        
        if err:
            log_area.text(f"❌ Error fetching text: {err}. Skipping.")
            continue
            
        # B. Analyze Document Structure (Find the Offset)
        offset_start = find_page_one_offset(current_text)
        if offset_start:
            log_area.text(f"📏 Document Offset Found: Physical Page 1 is PDF Page {offset_start}")
        else:
            log_area.text(f"⚠️ No 'Page 1' anchor found. Assuming purely sequential.")
            
        # C. Find the first unprocessed tag {{ocr}}
        # This function also calculates the CORRECT Roman/Arabic label
        pdf_filename, pdf_page_num, correct_label, old_tag_text = find_next_unprocessed_tag(current_text, offset_start)
        
        if not pdf_filename:
            log_area.text(f"✅ No '{{ocr}}' tags found in {wiki_title}. Marking complete.")
            save_state(wiki_title) # Save progress
            continue
            
        log_area.text(f"🎯 Target: PDF Page {pdf_page_num} -> New Label '{correct_label}'")

        # D. Find Local PDF
        local_path = pdf_index.get(pdf_filename)
        if not local_path:
            log_area.text(f"⚠️ PDF '{pdf_filename}' not found locally. Skipping.")
            continue
            
        # E. Extract Image
        img = get_page_image_data(local_path, pdf_page_num)
        if not img:
            log_area.text(f"❌ Failed to extract image. Skipping.")
            continue
            
        # F. AI Processing
        final_text = ""
        try:
            if ocr_strategy == "Gemini (Default)":
                log_area.text("✨ Gemini Processing...")
                final_text = proofread_with_formatting(img)
                if "GEMINI_ERROR" in final_text:
                    log_area.text("⚠️ Fallback to DocAI...")
                    raw_ocr = transcribe_with_document_ai(img)
                    if "DOCAI_ERROR" in raw_ocr:
                        st.error(f"Fallback Failed: {raw_ocr}")
                        continue
                    final_text = reformat_raw_text(raw_ocr)
            else:
                log_area.text("🤖 DocAI Processing...")
                raw_ocr = transcribe_with_document_ai(img)
                if "DOCAI_ERROR" in raw_ocr:
                    st.error(f"DocAI Failed: {raw_ocr}")
                    continue
                final_text = reformat_raw_text(raw_ocr)
                
            if not final_text or "ERROR" in final_text:
                st.error(f"Processing failed: {final_text}")
                continue

            # G. RECONSTRUCT THE TAG & INJECT
            # We must remove the {{ocr}} placeholder and update the label
            
            # 1. Build new tag
            new_tag = f"{{{{page|{correct_label}|file={pdf_filename}|page={pdf_page_num}}}}}"
            
            # 2. Update the text locally first to replace the Old Tag AND the {{ocr}}
            #    find_next_unprocessed_tag returned the text of the tag. 
            #    We need to replace "Old_Tag + space + {{ocr}}" with "New_Tag + \n + Content"
            
            # Search for the specific instance we found earlier
            # Note: This is a simple replace, it might replace duplicates if the page has identical duplicates (unlikely for tags)
            
            replacement_pattern = re.compile(re.escape(old_tag_text) + r'\s*\{\{ocr\}\}', re.IGNORECASE)
            
            # Prepare content block
            replacement_block = f"{new_tag}\n{final_text}\n"
            
            new_wikitext = replacement_pattern.sub(replacement_block, current_text, count=1)
            
            # H. Upload
            log_area.text("💾 Uploading to Wiki...")
            res = upload_to_bahaiworks(wiki_title, new_wikitext, f"Bot: Proofread {correct_label} (PDF {pdf_page_num})")
            
            if res.get('edit', {}).get('result') == 'Success':
                st.success(f"✅ Completed Page {correct_label} of {wiki_title}")
                # Don't save state yet if we want to loop through ALL pages in this book
                # But for safety, we often save state. 
                # If you want to process MULTIPLE pages per Wiki Page, remove the 'break' below
                # and put this whole logic inside a while loop for the specific Wiki Page.
                
                # For now, we process ONE segment per run-loop to be safe.
                # Next loop iteration will pick up the same Wiki Title, find the NEXT {{ocr}}, and continue.
                # So we should NOT increment the main loop index if there are more {{ocr}} tags?
                # Complex. For now, let's just save state and move to next file. 
                # Better approach: Loop internally until no {{ocr}} left?
                save_state(wiki_title) 
            else:
                st.error(f"Upload API Failed: {res}")
        
        except Exception as e:
            st.error(f"Exception: {e}")
            continue
            
        processed_count += 1
        progress_bar.progress((i + 1 - start_index) / (end_index - start_index))
        
        if run_mode.startswith("Test"):
            st.info("Test Mode: Stopping after 1 segment.")
            break
            
        time.sleep(1)

    st.success(f"Sweep Complete! Processed {processed_count} segments.")
