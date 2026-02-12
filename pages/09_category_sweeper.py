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

def parse_page_template(wikitext):
    """
    Extracts the PDF filename and page number from the {{page}} template.
    Expected format examples: 
    {{page|i|file=MyFile.pdf|page=1}}
    {{page|file=MyFile.pdf|page=1}}
    """
    # Regex to find 'file=' and 'page=' parameters inside {{page...}}
    # We look for {{page ... }} and extract params
    
    # 1. Find the tag
    match = re.search(r'\{\{page\|(.*?)\}\}', wikitext, re.IGNORECASE | re.DOTALL)
    if not match:
        return None, None
        
    params_str = match.group(1)
    
    # 2. Extract File
    file_match = re.search(r'file\s*=\s*([^|]+)', params_str)
    # 3. Extract Page
    page_match = re.search(r'page\s*=\s*(\d+)', params_str)
    
    pdf_filename = file_match.group(1).strip() if file_match else None
    page_num = int(page_match.group(1)) if page_match else None
    
    return pdf_filename, page_num

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
st.markdown("Autonomously iterates through the maintenance category, finds local PDFs, and proofreads.")

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
                start_index = i + 1 # Start at the next one
                break
    
    # 4. Processing Loop
    processed_count = 0
    
    # Define end index based on run mode
    end_index = len(members)
    
    progress_bar = st.progress(0)
    
    for i in range(start_index, end_index):
        # Stop Check
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
            
        # B. Parse Template
        pdf_filename, pdf_page_num = parse_page_template(current_text)
        
        if not pdf_filename or not pdf_page_num:
            log_area.text(f"⚠️ Could not parse {{page}} template in {wiki_title}. Skipping.")
            continue
            
        # C. Find Local PDF
        local_path = pdf_index.get(pdf_filename)
        
        if not local_path:
            log_area.text(f"⚠️ PDF '{pdf_filename}' not found in local index. Skipping.")
            continue
            
        # D. Extract Image
        log_area.text(f"📸 Extracting Page {pdf_page_num} from {pdf_filename}...")
        img = get_page_image_data(local_path, pdf_page_num)
        
        if not img:
            log_area.text(f"❌ Failed to extract image from PDF. Skipping.")
            continue
            
        # E. AI Processing (Gemini -> DocAI Fallback)
        final_text = ""
        
        try:
            # STRATEGY: Gemini First?
            if ocr_strategy == "Gemini (Default)":
                log_area.text("✨ Asking Gemini to proofread...")
                final_text = proofread_with_formatting(img)
                
                # Check for Fallback conditions
                if "GEMINI_ERROR" in final_text:
                    log_area.text("⚠️ Gemini Error/Copyright. Falling back to DocAI...")
                    # Fallback Routine
                    raw_ocr = transcribe_with_document_ai(img)
                    if "DOCAI_ERROR" in raw_ocr:
                        st.error(f"Fallback Failed: {raw_ocr}")
                        continue
                    final_text = reformat_raw_text(raw_ocr)
            else:
                # STRATEGY: DocAI Only
                log_area.text("🤖 DocAI Direct Mode...")
                raw_ocr = transcribe_with_document_ai(img)
                if "DOCAI_ERROR" in raw_ocr:
                    st.error(f"DocAI Failed: {raw_ocr}")
                    continue
                final_text = reformat_raw_text(raw_ocr)
                
            # Verify we have content
            if not final_text or "ERROR" in final_text:
                st.error(f"Processing failed for {wiki_title}: {final_text}")
                continue
                
            # F. Inject & Upload
            log_area.text("💾 Uploading to Wiki...")
            
            # Use the existing injection logic which handles {{page}} tag borders
            new_wikitext, inject_err = inject_text_into_page(current_text, pdf_page_num, final_text, pdf_filename)
            
            if inject_err:
                st.error(f"Injection Error: {inject_err}")
                continue
                
            # Upload
            res = upload_to_bahaiworks(wiki_title, new_wikitext, "Automated Proofread (Category Sweep)")
            
            if res.get('edit', {}).get('result') == 'Success':
                st.success(f"✅ Completed: {wiki_title}")
                save_state(wiki_title)
            else:
                st.error(f"Upload API Failed: {res}")
        
        except Exception as e:
            st.error(f"Exception processing {wiki_title}: {e}")
            continue
            
        # Update UI
        processed_count += 1
        progress_bar.progress((i + 1 - start_index) / (end_index - start_index))
        
        # Test Mode Check
        if run_mode.startswith("Test"):
            st.info("Test Mode: Stopping after 1 file.")
            break
            
        time.sleep(1) # Polite API pause

    st.success(f"Sweep Complete! Processed {processed_count} pages.")
