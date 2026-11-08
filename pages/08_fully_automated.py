import streamlit as st
import os
import sys
import json
import re
import time
import fitz  # PyMuPDF
import requests
import concurrent.futures
import math

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.batch_worker import process_single_page, _fitz_lock
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
# --- CUSTOM TEMPLATE CONFIGURATION ---
CUSTOM_PAGE_CONFIGS = {
    "swpage": {
        "module_title": "Module:SotW",
        "file_format": "SW_Volume{vol}.pdf",
        "offset_map_name": "pdfOffset_map"
    },
    "bwpage": {
        "module_title": "Module:BahaiWorld", 
        "file_format": "BW_Volume{vol}.pdf", 
        "offset_map_name": "pdfOffset_map"
    }
}

def get_module_offsets(module_title, map_name, session):
    """Fetches the Lua module and extracts the offset map into a Python dict."""
    wikitext, error = fetch_wikitext(module_title, session=session)
    if error:
        st.error(f"Failed to fetch {module_title}: {error}")
        return {}

    offsets = {}
    # Find the table block: local pdfOffset_map = { ... }
    table_match = re.search(fr'local\s+{map_name}\s*=\s*\{{([^}}]+)\}}', wikitext, re.IGNORECASE)
    if table_match:
        table_content = table_match.group(1)
        # Find all [key]="value" or [key]=value pairs
        pairs = re.finditer(r'\[(\d+)\]\s*=\s*["\']?(-?\d+)["\']?', table_content)
        for p in pairs:
            offsets[int(p.group(1))] = int(p.group(2))
    return offsets

def normalize_page_templates(wikitext, session):
    """Finds custom templates and converts them to standard {{page|...}} tags."""
    offset_caches = {}

    for template_name, config in CUSTOM_PAGE_CONFIGS.items():
        # Matches {{swpage|11|105}} -> group 1: vol, group 2: page
        pattern = fr'\{{\{{\s*{template_name}\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\}}\}}'

        def repl(match):
            vol = int(match.group(1))
            page = int(match.group(2))

            # Lazy load the module offset map only if needed
            if template_name not in offset_caches:
                offset_caches[template_name] = get_module_offsets(
                    config["module_title"],
                    config["offset_map_name"],
                    session
                )

            offset = offset_caches[template_name].get(vol, 0)
            pdf_page = page + offset
            filename = config["file_format"].format(vol=vol)

            return f"{{{{page|{page}|file={filename}|page={pdf_page}}}}}"

        wikitext = re.sub(pattern, repl, wikitext, flags=re.IGNORECASE)

    return wikitext

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
    """Determines if a page should be Roman (i, ii) or Arabic (1, 2)."""
    if anchor_pdf_page is None:
        return str(pdf_page_num)
        
    if pdf_page_num < anchor_pdf_page:
        return int_to_roman(pdf_page_num)
    else:
        return str(pdf_page_num - anchor_pdf_page + 1)

def find_anchor_offset(wikitext):
    """Scans for any {{page|N|...|page=X}} where N is an integer to establish the offset."""
    tags = re.finditer(r'\{\{page\|(.*?)\}\}', wikitext, re.IGNORECASE | re.DOTALL)
    
    for match in tags:
        params = match.group(1)
        label = params.split('|')[0].strip()
        
        if label.isdigit():
            page_match = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
            if page_match:
                book_page = int(label)
                pdf_page = int(page_match.group(1))
                return pdf_page - book_page + 1
    return None

def find_and_fix_tag_by_page_num(wikitext, pdf_filename, pdf_page_num, correct_label):
    """Robustly finds {{page|...|file=...|page=X}} regardless of the existing label."""
    tags = list(re.finditer(r'(\{\{page\|(.*?)\}\})(\s*\{\{ocr\}\})?', wikitext, re.IGNORECASE | re.DOTALL))
    
    for match in tags:
        full_tag_block = match.group(0)
        params = match.group(2)
        
        file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
        page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
        
        if file_check and page_check:
            found_filename = file_check.group(1).strip()
            if int(page_check.group(1)) == pdf_page_num and \
               os.path.basename(found_filename).lower() == os.path.basename(pdf_filename).lower():
                new_tag = f"{{{{page|{correct_label}|file={pdf_filename}|page={pdf_page_num}}}}}"
                wikitext = wikitext.replace(full_tag_block, new_tag)
                return wikitext
    return wikitext

def build_local_pdf_map(root_folder):
    """Recursively finds all PDF files and maps lowercase filename -> full local path."""
    pdf_map = {}
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower().endswith(".pdf"):
                # Normalize spaces/underscores in keys so MediaWiki titles match local files
                norm_key = f.lower().replace(" ", "_")
                pdf_map[norm_key] = os.path.join(dirpath, f)
    return pdf_map

def get_all_wiki_text_pages(base_wiki_title, session=None):
    """Queries MediaWiki API for all subpages under base_wiki_title ending in /Text."""
    req_session = session or requests
    prefix = base_wiki_title.replace("_", " ").strip()
    if not prefix.endswith("/"):
        prefix += "/"
        
    pages = []
    apcontinue = None
    
    while True:
        params = {
            "action": "query",
            "list": "allpages",
            "apprefix": prefix,
            "apnamespace": 0,
            "aplimit": "max",
            "format": "json"
        }
        if apcontinue:
            params["apcontinue"] = apcontinue
            
        res = req_session.get(API_URL, params=params, timeout=30).json()
        for p in res.get("query", {}).get("allpages", []):
            title = p["title"]
            if title.endswith("/Text"):
                pages.append(title)
                
        if "continue" in res:
            apcontinue = res["continue"]["apcontinue"]
        else:
            break
            
    # Natural sort (so Volume 2 / Issue 2 comes before Volume 10 / Issue 10)
    pages.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])
    return pages

def extract_pdf_filename_from_wikitext(wikitext):
    """Extracts the PDF filename from the first {{page|...|file=FILENAME.pdf|...}} tag."""
    match = re.search(r'\{\{page\|[^}]*?file\s*=\s*([^|}\n]+)', wikitext, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

def render_ocr_telemetry(pages_to_process, page_futures, shared_results, shared_log_list, ui_holders):
    """Updates the live metric cards, straggler radar, full-height color-coded page grid, and filtered warning log."""
    total_pages = len(pages_to_process)
    docai_pages = set()
    error_pages = set()
    skipped_cached_pages = set()
    warning_logs = []

    for msg in shared_log_list:
        if any(w in msg for w in ["⚠️", "❌", "🚨", "🤖"]):
            warning_logs.append(msg)
        m = re.search(r'Page\s+(\d+)', msg)
        if m:
            p = int(m.group(1))
            if "Falling back to DocAI" in msg or "DocAI Only Mode" in msg:
                docai_pages.add(p)
            if "❌ SKIPPING" in msg or "🚨" in msg:
                error_pages.add(p)
            if "⏩ Skipping" in msg:
                skipped_cached_pages.add(p)

    completed_count = 0
    running_pages = []
    unfinished_pages = []
    badges_html = []

    for p in pages_to_process:
        f = page_futures[p]
        val = shared_results.get(str(p))

        if f.done():
            completed_count += 1
            if p in error_pages or (val == "" and str(p) in shared_results):
                bg, fg, border, title = "#ffebee", "#c62828", "#ef9a9a", "Error / Failed"
            elif p in docai_pages:
                bg, fg, border, title = "#fff8e1", "#f57f17", "#ffe082", "DocAI Fallback"
            elif p in skipped_cached_pages:
                bg, fg, border, title = "#e0f2f1", "#00695c", "#80cbc4", "Resumed from Cache"
            else:
                bg, fg, border, title = "#e8f5e9", "#2e7d32", "#a5d6a7", "Gemini Success"
        elif f.running():
            running_pages.append(p)
            unfinished_pages.append(p)
            bg, fg, border, title = "#e3f2fd", "#1565c0", "#90caf9", "Running Now"
        else:
            unfinished_pages.append(p)
            bg, fg, border, title = "#f5f5f5", "#757575", "#e0e0e0", "Queued"

        badges_html.append(
            f'<span title="Page {p}: {title}" style="display:inline-block; width:38px; text-align:center; '
            f'margin:2px; padding:3px 0; border-radius:4px; font-size:11px; font-weight:600; font-family:monospace; '
            f'background:{bg}; color:{fg}; border:1px solid {border};">{p}</span>'
        )

    ui_holders["progress"].progress(completed_count / total_pages if total_pages > 0 else 1.0)

    ui_holders["m_done"].metric("✅ Completed", f"{completed_count} / {total_pages}")
    ui_holders["m_flight"].metric("⚡ Running Now", len(running_pages))
    ui_holders["m_docai"].metric("🤖 DocAI Fallbacks", len(docai_pages))
    ui_holders["m_err"].metric("❌ Errors", len(error_pages))

    if 0 < len(unfinished_pages) <= 15:
        straggler_str = ", ".join(str(p) for p in unfinished_pages)
        ui_holders["stragglers"].info(f"⏳ **Waiting on final {len(unfinished_pages)} page(s):** `{straggler_str}`")
    else:
        ui_holders["stragglers"].empty()

    legend = (
        '<div style="margin-bottom:6px; font-size:12px;">'
        '<b>Legend:</b> '
        '<span style="background:#e8f5e9;color:#2e7d32;padding:2px 6px;border-radius:4px;border:1px solid #a5d6a7;">🟩 Gemini</span> '
        '<span style="background:#e0f2f1;color:#00695c;padding:2px 6px;border-radius:4px;border:1px solid #80cbc4;">🧼 Resumed</span> '
        '<span style="background:#fff8e1;color:#f57f17;padding:2px 6px;border-radius:4px;border:1px solid #ffe082;">🟨 DocAI Fallback</span> '
        '<span style="background:#e3f2fd;color:#1565c0;padding:2px 6px;border-radius:4px;border:1px solid #90caf9;">🟦 Running</span> '
        '<span style="background:#f5f5f5;color:#757575;padding:2px 6px;border-radius:4px;border:1px solid #e0e0e0;">⬜ Queued</span> '
        '<span style="background:#ffebee;color:#c62828;padding:2px 6px;border-radius:4px;border:1px solid #ef9a9a;">🟥 Error</span>'
        '</div>'
    )
    grid_box = (
        f'{legend}<div style="line-height:1.4; padding:8px; '
        f'border:1px solid rgba(128,128,128,0.25); border-radius:6px;">{"".join(badges_html)}</div>'
    )
    ui_holders["grid"].markdown(grid_box, unsafe_allow_html=True)

    if warning_logs:
        ui_holders["warnings"].code("\n".join(warning_logs[-100:]), language="text")
    else:
        ui_holders["warnings"].caption("🎉 No warnings, blocks, or DocAI fallbacks triggered so far.")

    return completed_count == total_pages

# ==============================================================================
# 2. STATE MANAGEMENT
# ==============================================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            if 'current_page_num' in state:
                del state['current_page_num']
            return state
    return {"current_file_index": 0, "status": "idle", "last_processed": None}

def save_state(file_index, status, last_file_path=None):
    state = {
        "current_file_index": file_index,
        "status": status,
        "last_processed": last_file_path
    }
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def reset_state():
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    return {"current_file_index": 0, "status": "idle", "last_processed": None}

# ==============================================================================
# 3. UI & MAIN LOGIC
# ==============================================================================

if __name__ == '__main__':
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

    state_display = st.sidebar.empty()
    state_display.markdown(f"**Current State:**\n- Wiki Page Index: `{state['current_file_index']}`")

    # --- Manual State Modification ---
    with st.sidebar.expander("🔧 Modify Position"):
        # Input fields initialized with current state values
        new_file_index = st.number_input("Wiki Page Index (0-based)", min_value=0, value=state['current_file_index'])
        
        if st.button("Update State"):
            # Update the JSON file with your manual values
            save_state(new_file_index, "manual_override", last_file_path=state.get('last_processed'))
            st.sidebar.success("State updated!")
            time.sleep(0.5) 
            st.rerun()

    # --- Main Area ---

    if not input_folder or not os.path.exists(input_folder):
        st.warning("Please provide a valid local folder path.")
        st.stop()

    if not base_title.strip():
        st.warning("Please provide a valid Base Wiki Title.")
        st.stop()

    # 1. Build Local PDF Map & Query Wiki Pages
    local_pdf_map = build_local_pdf_map(input_folder)
    if not local_pdf_map:
        st.error("No PDF files found in the local directory.")
        st.stop()

    with st.spinner(f"🔍 Searching Bahai.works for `{base_title}` subpages..."):
        wiki_pages = get_all_wiki_text_pages(base_title)
    total_pages_count = len(wiki_pages)

    if total_pages_count == 0:
        st.error(f"No `/Text` subpages found on Wiki under `{base_title}`.")
        st.stop()

    # --- GUARD CLAUSE ---
    if state['current_file_index'] >= total_pages_count:
        st.warning(f"⚠️ Saved index ({state['current_file_index']}) is larger than total Wiki pages ({total_pages_count}). Resetting start position to 0.")
        state['current_file_index'] = 0
        # Save the corrected state immediately
        save_state(0, "auto_reset")

    st.info(f"🌐 Found **{total_pages_count}** `/Text` pages on Wiki under `{base_title}` (and **{len(local_pdf_map)}** local PDFs indexed).")

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
        
        # If resuming, we start from the index in state
        # If "Test Mode" is on, we only loop ONCE.
        end_idx = current_idx + 1 if run_mode.startswith("Test") else total_pages_count

        for idx in range(current_idx, end_idx):
            wiki_title = wiki_pages[idx]
            
            status_container.markdown(f"### 🔨 Processing Wiki Page {idx+1}/{total_pages_count}: `{wiki_title}`")

            # 4a. Fetch Live Wikitext First to Discover Required PDF
            log_area.text(f"🌐 Fetching live text from {wiki_title}...")
            max_retries = 3
            for attempt in range(max_retries):
                live_wikitext, error = fetch_wikitext(wiki_title, session=session)
                if not error:
                    break
                if attempt < max_retries - 1:
                    time.sleep(30)
            if error:
                st.error(f"CRITICAL ERROR: Could not fetch '{wiki_title}'. Error: {error}")
                st.stop()

            live_wikitext = normalize_page_templates(live_wikitext, session)

            # 4b. Extract PDF filename from {{page|...|file=...}} and match locally
            wiki_pdf_name = extract_pdf_filename_from_wikitext(live_wikitext)
            if not wiki_pdf_name:
                st.warning(f"⚠️ No `{{{{page|...|file=...}}}}` tag found on `{wiki_title}`. Skipping.")
                save_state(idx + 1, "running", last_file_path=wiki_title)
                continue

            lookup_key = wiki_pdf_name.lower().replace(" ", "_")
            pdf_path = local_pdf_map.get(lookup_key)

            if not pdf_path:
                st.warning(f"⚠️ Wiki page `{wiki_title}` expects `{wiki_pdf_name}`, which was not found in `{input_folder}`. Skipping.")
                save_state(idx + 1, "running", last_file_path=wiki_title)
                continue

            # Use the exact filename found on disk for processing, or wiki_pdf_name for tag matching
            short_name = os.path.basename(pdf_path)
            status_container.caption(f"Matched Local PDF: `{short_name}` (Wiki `file={wiki_pdf_name}`)")

            # --- Local Working Copy Setup ---
            wip_file_path = os.path.join(project_root, f"wip_{short_name}.txt")

            if not os.path.exists(wip_file_path):
                # Save the initial fetch to our local working file
                with open(wip_file_path, "w", encoding="utf-8") as f:
                    f.write(live_wikitext)

                save_state(idx, "merging", last_file_path=short_name)
                state_display.markdown(f"**Current State:**\n- Wiki Page Index: `{idx}`")

            else:
                log_area.text(f"📂 Resuming from local working copy for {short_name}...")

            # 4c. Parallel Page Processing (100 Workers + Live Telemetry)
            with fitz.open(pdf_path) as doc:
                total_pages = len(doc)

            pages_to_process = list(range(1, total_pages + 1))
            
            if not pages_to_process:
                log_area.text(f"No pages left to process for {short_name}.")
                continue

            temp_master_path = os.path.join(project_root, f"temp_master_{short_name}.json")
            shared_results = {}
            if os.path.exists(temp_master_path):
                try:
                    with open(temp_master_path, "r", encoding="utf-8") as f:
                        loaded_temp = json.load(f)
                        shared_results = {str(k): v for k, v in loaded_temp.items() if v}
                except json.JSONDecodeError:
                    shared_results = {}

            ocr_box = st.container(border=True)
            if shared_results:
                ocr_box.info(f"🔄 Resuming from checkpoint: **{len(shared_results)} / {len(pages_to_process)}** pages already saved.")

            ocr_progress_bar = ocr_box.progress(0)
            m_cols = ocr_box.columns(4)
            straggler_holder = ocr_box.empty()
            grid_holder = ocr_box.empty()
            with ocr_box.expander("⚠️ Warnings & DocAI Fallbacks Log", expanded=True):
                warnings_holder = st.empty()

            ui_holders = {
                "progress": ocr_progress_bar,
                "m_done": m_cols[0].empty(),
                "m_flight": m_cols[1].empty(),
                "m_docai": m_cols[2].empty(),
                "m_err": m_cols[3].empty(),
                "stragglers": straggler_holder,
                "grid": grid_holder,
                "warnings": warnings_holder,
            }

            for attempt in range(2):
                fatal_error = False
                fatal_error_msg = ""
                shared_log_list = []

                pdf_doc = fitz.open(pdf_path)
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=100)
                page_futures = {
                    page_num: executor.submit(
                        process_single_page,
                        page_num, pdf_doc, ocr_strategy,
                        temp_master_path, shared_results, shared_log_list
                    )
                    for page_num in pages_to_process
                }

                while True:
                    all_done = render_ocr_telemetry(
                        pages_to_process, page_futures, shared_results, shared_log_list, ui_holders
                    )
                    for log_msg in list(shared_log_list):
                        if "FATAL_RATE_LIMIT" in log_msg:
                            fatal_error = True
                            fatal_error_msg = log_msg
                            break
                    if fatal_error or all_done:
                        break
                    time.sleep(1)

                executor.shutdown(wait=False, cancel_futures=True)
                with _fitz_lock:
                    if not pdf_doc.is_closed:
                        pdf_doc.close()

                if fatal_error:
                    if attempt == 0:
                        ocr_box.warning(f"⚠️ Fatal API/Network error detected: `{fatal_error_msg}`. Sleeping for 15 minutes before retrying once...")
                        retry_countdown = ocr_box.empty()
                        for remaining in range(900, 0, -1):
                            mins, secs = divmod(remaining, 60)
                            retry_countdown.code(f"Retrying OCR in: {mins:02d}:{secs:02d}")
                            time.sleep(1)
                        retry_countdown.empty()
                    else:
                        st.error(f"❌ FATAL (Failed twice): `{fatal_error_msg}`. Halting execution.")
                        st.stop()
                else:
                    break

            # 4d. Sequential Merge & Wiki Injection
            log_area.text(f"🔄 Merging extracted pages and injecting wikitext for {short_name}...")
            
            all_extracted_text = {
                int(p_num_str): text
                for p_num_str, text in shared_results.items()
                if text and text.strip()
            }

            # SAFETY GUARD: Never touch the Wiki if no pages were extracted!
            if not all_extracted_text:
                st.error(f"CRITICAL SAFETY STOP: 0 pages were extracted for `{short_name}`. Aborting before modifying `{wiki_title}`.")
                st.stop()

            # Sequentially inject each page into the live wikitext
            with open(wip_file_path, "r", encoding="utf-8") as f:
                current_wikitext = f.read()

            # --- Calculate the anchor offset for Roman Numeral logic ---
            anchor_pdf_page = find_anchor_offset(current_wikitext)

            save_state(idx, "merging", last_file_path=short_name)

            for page_num in pages_to_process:
                if stop_info:
                    st.warning("Stopping requested... finishing current document.")
                    break

                final_text = all_extracted_text.get(page_num, "").strip()
                
                # --- Calculate label and fix tags before injection ---
                correct_label = calculate_page_label(page_num, anchor_pdf_page)
                current_wikitext = find_and_fix_tag_by_page_num(current_wikitext, wiki_pdf_name, page_num, correct_label)

                if not final_text:
                    log_area.text(f"⚠️ Page {page_num} was empty or failed. Skipping injection.")
                    save_state(idx, "merging", last_file_path=short_name)
                    continue

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
                final_wikitext, inject_error = inject_text_into_page(current_wikitext, correct_label, final_text, wiki_pdf_name)
                
                if inject_error:
                    st.error(f"CRITICAL ERROR injecting {short_name} Page {page_num}: {inject_error}")
                    st.stop()
                    
                current_wikitext = final_wikitext
                
                # Save local WIP step-by-step just in case
                with open(wip_file_path, "w", encoding="utf-8") as f:
                    f.write(current_wikitext)

            # --- SAFE FINAL CLEANUP & UPLOAD (Outside the page loop) ---
            if "__NOTOC__" not in current_wikitext:
                current_wikitext = current_wikitext.rstrip() + "\n__NOTOC__\n"

            log_area.text(f"🧹 Running final seam cleanup on {short_name}...")
            cleaned_text = cleanup_page_seams(current_wikitext)
            
            # Convert short youtu.be links to full youtube.com URLs to bypass spam filter
            cleaned_text = re.sub(r'https?://(?:www\.)?youtu\.be/([a-zA-Z0-9_-]+)', r'https://www.youtube.com/watch?v=\1', cleaned_text)
            
            log_area.text(f"🚀 Uploading completed issue to Bahai.works...")
            summary = f"Automated Proofread: {short_name} (Full Issue)"
            res = upload_to_bahaiworks(wiki_title, cleaned_text, summary, session=session)
            
            if res.get('edit', {}).get('result') != 'Success':
                st.error(f"UPLOAD FAILED: {res}")
                st.stop()
            
            if os.path.exists(wip_file_path):
                os.remove(wip_file_path)
            
            # Clean up temp master checkpoint file only after successful upload
            if os.path.exists(temp_master_path):
                os.remove(temp_master_path)

            # Update State (Success for entire file)
            save_state(idx + 1, "running", last_file_path=short_name)
            state_display.markdown(f"**Current State:**\n- Wiki Page Index: `{idx + 1}`")
            with status_container:
                st.success(f"✅ Finished Document: {short_name}")

            if stop_info:
                break

        # End of Loop
        st.success("🎉 Batch Processing Complete!")
        save_state(end_idx, "done", last_file_path=short_name if 'short_name' in locals() else None)
        if 'short_name' in locals():
            state_display.markdown(f"**Current State:**\n- Wiki Page Index: `{end_idx}`")
