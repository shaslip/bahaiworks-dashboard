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
import concurrent.futures
import math
import multiprocessing

# --- Force spawn to prevent gRPC crashes in background processes ---
try:
    multiprocessing.set_start_method('spawn', force=True)
except RuntimeError:
    pass

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
    cleanup_page_seams,
    get_csrf_token,
    update_header_ps_tag
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

def process_pdf_batch(batch_id, page_list, pdf_path, ocr_strategy, short_name, project_root, shared_log_list):
    gemini_consecutive_failures = 0
    docai_cooldown_pages = 0
    permanent_docai = False
    
    batch_file_path = os.path.join(project_root, f"temp_{short_name}_batch_{batch_id}.json")
    batch_results = {}
    
    if os.path.exists(batch_file_path):
        try:
            with open(batch_file_path, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                batch_results = {int(k): v for k, v in saved_data.items()}
        except json.JSONDecodeError:
            pass

    for page_num in page_list:
        if page_num in batch_results and batch_results[page_num] and "ERROR" not in batch_results[page_num]:
            shared_log_list.append(f"⏩ Skipping Page {page_num} (Already processed)")
            continue

        # (Removed the "📄 Reading Page..." log here entirely)

        img = get_page_image_data(pdf_path, page_num)
        if img is None:
            continue
            
        final_text = ""
        force_docai = (ocr_strategy == "DocAI Only") or permanent_docai or (docai_cooldown_pages > 0)

        if force_docai:
            mode_label = "Permanent DocAI" if permanent_docai else f"Cooldown DocAI ({docai_cooldown_pages} left)"
            shared_log_list.append(f"🤖 [{mode_label}] Processing Page {page_num}...")
            
            raw_ocr = transcribe_with_document_ai(img)
            if "DOCAI_ERROR" in raw_ocr:
                shared_log_list.append(f"⚠️ DocAI Failed. Attempting Gemini Rescue...")
                final_text = proofread_with_formatting(img)
            else:
                final_text = reformat_raw_text(raw_ocr)
                if "FORMATTING_ERROR" in final_text:
                    shared_log_list.append(f"⚠️ DocAI Formatting Failed. Attempting Gemini Rescue...")
                    rescue_text = proofread_with_formatting(img)
                    if "GEMINI_ERROR" in rescue_text or "Recitation" in rescue_text:
                        shared_log_list.append(f"⚠️ Rescue also failed. Saving RAW OCR text.")
                        final_text = raw_ocr + "\n\n"
                    else:
                        final_text = rescue_text

        if docai_cooldown_pages > 0:
            docai_cooldown_pages -= 1
            if docai_cooldown_pages == 0:
                shared_log_list.append(f"🟢 Cooldown complete. Re-enabling Gemini.")

        else:
            final_text = proofread_with_formatting(img)
            is_gemini_error = "GEMINI_ERROR" in final_text or "Recitation" in final_text or "Copyright" in final_text

            if is_gemini_error:
                gemini_consecutive_failures += 1
                if gemini_consecutive_failures == 2:
                    docai_cooldown_pages = 5
                    shared_log_list.append(f"⚠️ 2 Consecutive Failures. Switching to DocAI for next 5 pages.")
                elif gemini_consecutive_failures >= 3:
                    permanent_docai = True
                    shared_log_list.append(f"⛔ 3rd Strike. Switching to DocAI for remainder of batch.")
                else:
                    shared_log_list.append(f"⚠️ Gemini Error ({gemini_consecutive_failures}/2). Retrying with DocAI...")

                raw_ocr = transcribe_with_document_ai(img)
                if "DOCAI_ERROR" in raw_ocr:
                    final_text = "DOCAI_ERROR" 
                else:
                    formatted_text = reformat_raw_text(raw_ocr)
                    if "FORMATTING_ERROR" in formatted_text:
                        shared_log_list.append(f"⚠️ Formatting failed. Saving RAW OCR text.")
                        final_text = raw_ocr + "\n\n"
                    else:
                        final_text = formatted_text
            else:
                gemini_consecutive_failures = 0

        system_error_flags = ["GEMINI_ERROR", "DOCAI_ERROR", "FORMATTING_ERROR"]
        if not final_text or any(flag in final_text for flag in system_error_flags):
            error_summary = final_text if final_text else "Empty Response"
            shared_log_list.append(f"❌ SKIPPING Page {page_num} due to failure: {error_summary}")
            batch_results[page_num] = "" 
        else:
            batch_results[page_num] = final_text
            # --- NEW: Re-added the local save confirmation ---
            shared_log_list.append(f"✅ Saved Page {page_num} locally")

        with open(batch_file_path, "w", encoding="utf-8") as f:
            json.dump(batch_results, f)

    return True

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
    # 1. SETUP SHARED SESSION
    session = requests.Session()
    try:
        with st.spinner("🔐 Authenticating with MediaWiki..."):
            get_csrf_token(session)
            st.success("Authenticated successfully!")
    except Exception as e:
        st.error(f"Authentication Failed: {e}")
        st.stop()

    current_idx = state['current_file_index']
    
    # If resuming, we start from the file index in state
    # If "Test Mode" is on, we only loop ONCE.
    end_idx = current_idx + 1 if run_mode.startswith("Test") else total_files

    for i in range(current_idx, end_idx):
        pdf_path = pdf_files[i]
        
        # 4a. Resolve Titles
        wiki_title = get_wiki_title(pdf_path, input_folder, base_title)
        
        if not wiki_title:
            st.error(f"Could not determine Wiki Title for {pdf_path}. Skipping.")
            continue

        short_name = os.path.basename(pdf_path)
        
        status_container.markdown(f"### 🔨 Processing File {i+1}/{total_files}: `{short_name}`")
        status_container.caption(f"Target Wiki Page: `{wiki_title}`")

        # --- NEW: Local Working Copy Setup ---
        wip_file_path = os.path.join(project_root, f"wip_{short_name}.txt")
        
        if not os.path.exists(wip_file_path):
            log_area.text(f"🌐 Fetching live text from {wiki_title} to start local editing...")
            max_retries = 3
            for attempt in range(max_retries):
                current_wikitext, error = fetch_wikitext(wiki_title, session=session)
                if not error:
                    break
                if attempt < max_retries - 1:
                    time.sleep(30)
            if error:
                st.error(f"CRITICAL ERROR: Could not fetch '{wiki_title}'. Error: {error}")
                st.stop()
            
            # Save the initial fetch to our local working file
            with open(wip_file_path, "w", encoding="utf-8") as f:
                f.write(current_wikitext)
        else:
            log_area.text(f"📂 Resuming from local working copy for {short_name}...")

        # 4c. Parallel Batch Processing
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        doc.close()

        # --- Check state for starting page ---
        if i == state['current_file_index']:
            start_page = state['current_page_num']
        else:
            start_page = 1

        pages_to_process = list(range(start_page, total_pages + 1))
        
        if not pages_to_process:
            log_area.text(f"No pages left to process for {short_name}.")
            continue

        # Split into 5 roughly equal batches
        num_batches = 5
        batch_size = math.ceil(len(pages_to_process) / num_batches)
        batches = [pages_to_process[j:j + batch_size] for j in range(0, len(pages_to_process), batch_size)]

        st.write(f"🚀 Starting parallel processing: {len(pages_to_process)} pages across {len(batches)} batches.")

        # --- UI SETUP FOR BATCH LOGGING ---
        batch_placeholders = {}
        
        for i in range(len(batches)):
            start_pg = batches[i][0]
            end_pg = batches[i][-1]
            
            # Format cleanly if a batch happens to only have 1 page
            page_label = f"pg {start_pg}" if start_pg == end_pg else f"pgs {start_pg}-{end_pg}"
            
            with st.expander(f"Batch {i+1} Status ({page_label})", expanded=True):
                batch_placeholders[i] = st.empty()

        # --- NEW: MULTIPROCESSING SETUP ---
        from multiprocessing import Manager
        
        with Manager() as manager:
            # Create a managed dictionary to hold managed lists for each batch
            shared_logs = manager.dict()
            for i in range(len(batches)):
                shared_logs[i] = manager.list()

            with st.spinner(f"Processing {short_name} in {len(batches)} parallel batches..."):
                # Swapped to ProcessPoolExecutor to bypass the Python GIL
                with concurrent.futures.ProcessPoolExecutor(max_workers=num_batches) as executor:
                    futures = []
                    for batch_id, page_list in enumerate(batches):
                        futures.append(
                            executor.submit(
                                process_pdf_batch, 
                                batch_id, 
                                page_list, 
                                pdf_path, 
                                ocr_strategy, 
                                short_name, 
                                project_root,
                                shared_logs[batch_id]  # Pass the managed list proxy
                            )
                        )

                    # --- REAL-TIME POLLING LOOP ---
                    while True:
                        all_done = True
                        for batch_id, future in enumerate(futures):
                            # Extract the managed list back into a standard list to read it safely
                            current_logs = list(shared_logs[batch_id])
                            if current_logs:
                                batch_placeholders[batch_id].text("\n".join(current_logs[-15:]))
                            
                            if not future.done():
                                all_done = False
                                
                        if all_done:
                            break
                            
                        time.sleep(1) # Refresh UI every 1 second

                    # Catch any process exceptions
                    for future in futures:
                        try:
                            future.result()
                        except Exception as e:
                            st.error(f"🚨 Process Exception: {str(e)}")
                            st.stop()

        # 4d. Sequential Merge & Wiki Injection
        log_area.text(f"🔄 Merging parallel batches and injecting wikitext for {short_name}...")
        
        # Load all temporary batch files into one dictionary
        all_extracted_text = {}
        for batch_id in range(len(batches)):
            batch_file_path = os.path.join(project_root, f"temp_{short_name}_batch_{batch_id}.json")
            if os.path.exists(batch_file_path):
                with open(batch_file_path, "r", encoding="utf-8") as f:
                    batch_data = json.load(f)
                    for p_num_str, text in batch_data.items():
                        all_extracted_text[int(p_num_str)] = text

        # Sequentially inject each page into the live wikitext
        with open(wip_file_path, "r", encoding="utf-8") as f:
            current_wikitext = f.read()

        save_state(i, pages_to_process[0], "merging", last_file_path=short_name)

        for page_num in pages_to_process:
            if stop_info:
                st.warning("Stopping requested... finishing current document.")
                break

            final_text = all_extracted_text.get(page_num, "")
            is_last_page = (page_num == total_pages)
            
            if not final_text:
                log_area.text(f"⚠️ Page {page_num} was empty or failed. Skipping injection.")
                # We still need to trigger the final upload if this skipped page was the last page
                if is_last_page:
                    pass # Let it fall through to the cleanup phase below
                else:
                    save_state(i, page_num + 1, "merging", last_file_path=short_name)
                    continue 

            if is_last_page and final_text:
                final_text += "\n__NOTOC__"

            # --- PAGE 1 SPECIAL HANDLING ---
            if page_num == 1:
                current_wikitext = re.sub(r'\{\{ocr.*?\}\}\n?', '', current_wikitext, flags=re.IGNORECASE)
                
                found_year = None
                cat_match = re.search(r'\[\[Category:\s*(\d{4})\s*\]\]', current_wikitext, re.IGNORECASE)
                if cat_match: found_year = cat_match.group(1)

                if "{{header" not in current_wikitext:
                    volume_found = None
                    issue_identifier = None 
                    
                    if "/Volume_" in wiki_title and "/Issue_" in wiki_title:
                        v_match = re.search(r'Volume_(\d+)', wiki_title)
                        i_match = re.search(r'Issue_(\d+)', wiki_title)
                        if v_match and i_match:
                            volume_found = v_match.group(1)
                            issue_identifier = i_match.group(1)
                    else:
                        i_match = re.search(r'Issue_([\d-]+)', wiki_title)
                        if i_match:
                            issue_identifier = i_match.group(1)
                        else:
                            fn_match = re.search(r'(\d+(?:-\d+)?)', short_name)
                            if fn_match: issue_identifier = fn_match.group(1)

                    if issue_identifier:
                        header = generate_header(issue_identifier, year=found_year, volume=volume_found)
                        access_match = re.match(r'^\s*<accesscontrol>.*?</accesscontrol>\s*', current_wikitext, re.DOTALL | re.IGNORECASE)
                        if access_match:
                            access_tag = access_match.group(0).strip()
                            remaining_body = current_wikitext[access_match.end():].lstrip()
                            current_wikitext = access_tag + "\n" + header + "\n" + remaining_body
                        else:
                            current_wikitext = header + "\n" + current_wikitext.lstrip()
            else:
                current_wikitext = update_header_ps_tag(current_wikitext)
            
            # Inject Content
            final_wikitext, inject_error = inject_text_into_page(current_wikitext, page_num, final_text, short_name)
            
            if inject_error:
                st.error(f"CRITICAL ERROR injecting {short_name} Page {page_num}: {inject_error}")
                st.stop()
                
            current_wikitext = final_wikitext
            
            # Save local WIP step-by-step just in case
            with open(wip_file_path, "w", encoding="utf-8") as f:
                f.write(current_wikitext)

            # --- SAFE FINAL CLEANUP & UPLOAD ---
            if is_last_page:
                log_area.text(f"🧹 Running final seam cleanup on {short_name}...")
                cleaned_text = cleanup_page_seams(current_wikitext)
                
                log_area.text(f"🚀 Uploading completed issue to Bahai.works...")
                summary = f"Automated Proofread: {short_name} (Full Issue)"
                res = upload_to_bahaiworks(wiki_title, cleaned_text, summary, session=session)
                
                if res.get('edit', {}).get('result') != 'Success':
                    st.error(f"UPLOAD FAILED: {res}")
                    st.stop()
                
                if os.path.exists(wip_file_path):
                    os.remove(wip_file_path)
                
                # Clean up temp batch files only after successful upload
                for batch_id in range(len(batches)):
                    batch_file_path = os.path.join(project_root, f"temp_{short_name}_batch_{batch_id}.json")
                    if os.path.exists(batch_file_path):
                        os.remove(batch_file_path)

        # Update State (Success for entire file)
        save_state(i + 1, 1, "running", last_file_path=short_name)
        with status_container:
            st.success(f"✅ Finished Document: {short_name}")

        if stop_info:
            break

    # End of Loop
    st.success("🎉 Batch Processing Complete!")
    save_state(end_idx, 1, "done", last_file_path=short_name)
