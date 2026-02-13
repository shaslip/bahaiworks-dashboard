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
from src.mediawiki_uploader import (
    upload_to_bahaiworks, 
    API_URL, 
    fetch_wikitext, 
    inject_text_into_page, 
    generate_header, 
    cleanup_page_seams
)

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

STATE_FILE = os.path.join(project_root, "automation_state.json")

st.set_page_config(page_title="Fully Automated Proofreader", page_icon="🤖", layout="wide")

# ==============================================================================
# 1. HELPER FUNCTIONS
# ==============================================================================

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
    Determines the Wiki Page Title based on filename and folder structure.
    """
    filename = os.path.basename(local_path)
    name_no_ext = os.path.splitext(filename)[0]

    try:
        rel_path = os.path.relpath(local_path, root_folder)
    except ValueError:
        rel_path = local_path

    # --- STRATEGY 0: Canadian Baha'i News Specific (Canadian_Bahai_News_24.pdf) ---
    # Matches "Canadian_Bahai_News_" followed by digits or digits-digits (24 or 24-25)
    cbn_match = re.search(r'Canadian_Bahai_News_(\d+(?:-\d+)?)', filename, re.IGNORECASE)
    if cbn_match:
        issue_identifier = cbn_match.group(1)
        # Note: Input filename is "Bahai", target wiki is "Bahá’í"
        # We assume base_wiki_title is passed correctly as "Canadian_Bahá’í_News"
        # or we force it if the file matches this specific pattern.
        target_base = "Canadian_Bahá’í_News" if "Canadian" in base_wiki_title else base_wiki_title
        return f"{target_base}/Issue_{issue_identifier}/Text"

    # --- STRATEGY 1: Explicit Filename (Vol 1 No 1) ---
    vol_issue_match = re.search(r'(?:Vol|Volume)[\W_]*(\d+)[\W_]*(?:No|Issue|Number)[\W_]*(\d+)', filename, re.IGNORECASE)
    if vol_issue_match:
        vol = int(vol_issue_match.group(1))
        issue = int(vol_issue_match.group(2))
        return f"{base_wiki_title}/Volume_{vol}/Issue_{issue}/Text"

    # --- STRATEGY 2: Folder Structure (Volume 1/...) ---
    parts = rel_path.split(os.sep)
    path_vol = None
    path_issue = None
    
    for part in parts:
        if not path_vol:
            v_match = re.search(r'^(?:Vol|Volume)[\W_]*(\d+)$', part, re.IGNORECASE)
            if v_match: path_vol = int(v_match.group(1))
        if not path_issue:
            i_match = re.search(r'^(?:No|Issue)[\W_]*(\d+)$', part, re.IGNORECASE)
            if i_match: path_issue = int(i_match.group(1))

    if path_vol:
        final_issue = path_issue
        if not final_issue:
            # Fallback: check filename for issue number
            num_match = re.search(r'(\d+)', filename)
            if num_match: final_issue = int(num_match.group(1))
        
        if final_issue:
            return f"{base_wiki_title}/Volume_{path_vol}/Issue_{final_issue}/Text"

    # --- STRATEGY 3: Implicit "04-01" (Volume-Issue) ---
    # Only triggers if leading zeros are present to avoid confusing "24-25" with "Vol 24 Issue 25"
    hyphen_vol_match = re.search(r'(\d+)-(\d+)', name_no_ext)
    
    if hyphen_vol_match:
        v_str = hyphen_vol_match.group(1)
        i_str = hyphen_vol_match.group(2)

        # Logic: If either part is clearly zero-padded, assume it is Vol-Issue structure.
        if v_str.startswith('0') or i_str.startswith('0'):
            vol_num = int(v_str)
            issue_num = int(i_str)
            return f"{base_wiki_title}/Volume_{vol_num}/Issue_{issue_num}/Text"

    # --- STRATEGY 4: Fallback "Name_24" or "Name_24-25" (No Volume) ---
    # If no other strategy hit, looks for the last number block in the filename
    # This catches "Some_Publication_24-25.pdf" as Issue 24-25
    simple_issue_match = re.search(r'(\d+(?:-\d+)?)', name_no_ext)
    if simple_issue_match:
        return f"{base_wiki_title}/Issue_{simple_issue_match.group(1)}/Text"

    return None

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
input_folder = st.sidebar.text_input("Local PDF Folder", value="/media/sarah/4TB/Projects/Bahai.works/English/Canada/1948-1975_CBN/")
base_title = st.sidebar.text_input("Base Wiki Title", value="Canadian_Bahá’í_News")

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

# --- GUARD CLAUSE ---
if state['current_file_index'] >= total_files:
    st.warning(f"⚠️ Saved index ({state['current_file_index']}) is larger than total files ({total_files}). Resetting start position to 0.")
    state['current_file_index'] = 0
    state['current_page_num'] = 1
    # Save the corrected state immediately
    save_state(0, 1, "auto_reset")

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
        
        # Safety check if title resolution failed
        if not wiki_title:
            st.error(f"Could not determine Wiki Title for {pdf_path}. Skipping.")
            continue

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
                        # --- Last Resort Fallback ---
                        log_area.text(f"⚠️ DocAI Formatting failed. Attempting Gemini fallback...")
                        
                        # Try Gemini directly as a Hail Mary
                        gemini_rescue_text = proofread_with_formatting(img)
                        
                        if "GEMINI_ERROR" not in gemini_rescue_text:
                            final_text = gemini_rescue_text
                            st.success(f"✨ Gemini successfully rescued Page {page_num}!")
                        else:
                            # Log warning instead of Error/Stop
                            st.warning(f"⚠️ DocAI Reformatter Failed AND Gemini Fallback Failed on Page {page_num}. Skipping page.")
                            
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
                    if "{{header" not in current_wikitext:
                        
                        # Initialize variables
                        volume_found = None
                        issue_identifier = None # Can be "1" or "24-25"

                        # Check for Volume/Issue format in the Title we just generated
                        if "/Volume_" in wiki_title and "/Issue_" in wiki_title:
                            # Extract from the clean Wiki path (Safer than filename)
                            v_match = re.search(r'Volume_(\d+)', wiki_title)
                            i_match = re.search(r'Issue_(\d+)', wiki_title)
                            
                            if v_match and i_match:
                                volume_found = v_match.group(1)
                                issue_identifier = i_match.group(1)
                        
                        else:
                            # Standard Issue or Range (Extract from Wiki Title is safest)
                            # wiki_title looks like ".../Issue_24-25/Text"
                            i_match = re.search(r'Issue_([\d-]+)', wiki_title)
                            if i_match:
                                issue_identifier = i_match.group(1)
                            else:
                                # Fallback to filename if Wiki Title is weird
                                fn_match = re.search(r'(\d+(?:-\d+)?)', short_name)
                                if fn_match: issue_identifier = fn_match.group(1)

                        # Only generate if we found an issue identifier
                        if issue_identifier:
                            header = generate_header(issue_identifier, year=found_year, volume=volume_found)
                            
                            # --- ACCESS CONTROL HANDLING ---
                            # Check for <accesscontrol> tag at start of file
                            access_match = re.match(r'^\s*<accesscontrol>.*?</accesscontrol>\s*', current_wikitext, re.DOTALL | re.IGNORECASE)
                            
                            if access_match:
                                access_tag = access_match.group(0).strip()
                                # Content AFTER the tag
                                remaining_body = current_wikitext[access_match.end():].lstrip()
                                # Reconstruction: Access Tag -> Header -> Body
                                current_wikitext = access_tag + "\n" + header + "\n" + remaining_body
                            else:
                                # Standard prepend
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
    save_state(end_idx, 1, "done", last_file_path=short_name)
