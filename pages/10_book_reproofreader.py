import streamlit as st
import os
import sys
import json
import re
import time
import requests
import fitz  # PyMuPDF
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
from src.batch_worker import process_pdf_batch
from src.mediawiki_uploader import (
    upload_to_bahaiworks, 
    API_URL, 
    fetch_wikitext, 
    inject_text_into_page, 
    get_csrf_token,
    cleanup_page_seams,
    update_header_ps_tag
)
from src.gemini_processor import apply_chunked_split

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

QUEUE_FILE = os.path.join(project_root, "book_sweeper_queue.json")
CACHE_DIR = os.path.join(project_root, "book_cache")
OFFLINE_DIR = os.path.join(project_root, "offline_proofs")

for d in [CACHE_DIR, OFFLINE_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

def apply_final_formatting(text, title, year):
    """Deletes {{ocr}} and injects or updates the {{header}}."""
    text = re.sub(r'\{\{ocr.*?\}\}\n?', '', text, flags=re.IGNORECASE)
    
    if "{{header" not in text.lower():
        section_name = title.split('/')[-1]
        cat_str = year if year else ""
        
        # Formatted to match the exact spacing and structure of the standard book subpage header
        header = f"""{{{{header
 | title      = [[../]]
 | author     = 
 | translator = 
 | section    = {section_name}
 | previous   = 
 | next       = 
 | notes      = {{{{ps|1}}}}
 | categories = {cat_str}
}}}}"""
        
        access_match = re.match(r'^\s*<accesscontrol>.*?</accesscontrol>\s*', text, re.DOTALL | re.IGNORECASE)
        if access_match:
            access_tag = access_match.group(0).strip()
            remaining_body = text[access_match.end():].lstrip()
            text = access_tag + "\n" + header + "\n" + remaining_body
        else:
            text = header + "\n" + text.lstrip()
    else:
        text = update_header_ps_tag(text)
        
    return text

st.set_page_config(page_title="Book Re-Proofreader", page_icon="📚", layout="wide")

# ==============================================================================
# 1. QUEUE & STATE MANAGEMENT
# ==============================================================================

def load_queue():
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_queue(queue_data):
    with open(QUEUE_FILE, 'w') as f:
        json.dump(queue_data, f, indent=4)

def load_book_state(safe_title):
    state_file = os.path.join(CACHE_DIR, f"{safe_title}_state.json")
    if os.path.exists(state_file):
        with open(state_file, 'r') as f:
            return json.load(f)
    return {"completed_subpages": [], "route_map": {}, "subpages": [], "master_pdf": None}

def save_book_state(safe_title, state):
    state_file = os.path.join(CACHE_DIR, f"{safe_title}_state.json")
    with open(state_file, 'w') as f:
        json.dump(state, f, indent=4)

# ==============================================================================
# 2. WIKI & MAP HELPERS
# ==============================================================================

def get_all_subpages(root_title, session):
    pages = [root_title]
    params = {
        "action": "query",
        "list": "allpages",
        "apprefix": f"{root_title}/",
        "aplimit": "500",
        "format": "json"
    }
    
    while True:
        try:
            res = session.get(API_URL, params=params).json()
            chunk = res.get('query', {}).get('allpages', [])
            pages.extend([p['title'] for p in chunk])
            
            if 'continue' in res:
                params.update(res['continue'])
            else:
                break
        except Exception as e:
            st.error(f"Network Error fetching subpages: {e}")
            break
            
    # Natural sort
    pages.sort(key=lambda x: [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', x)])
    return pages

def extract_page_content(wikitext, pdf_page_num):
    tags = re.finditer(r'(\{\{page\|(.*?)\}\})(.*?)(?=\{\{page\||\Z)', wikitext, re.IGNORECASE | re.DOTALL)
    for match in tags:
        params = match.group(2)
        content = match.group(3).strip()
        
        page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
        if page_check and int(page_check.group(1)) == pdf_page_num:
            content = re.sub(r'\{\{ocr\}\}\s*\Z', '', content, flags=re.IGNORECASE).strip()
            return content
    return ""

def build_sequential_route_map(subpages, session, input_folder):
    route_map = {}
    master_pdf_filename = None
    wikitext_cache = {}
    
    for title in subpages:
        text, err = fetch_wikitext(title, session=session)
        if err or not text:
            continue
            
        wikitext_cache[title] = text
        route_map[title] = {"pdf_pages": [], "old_texts": {}, "needs_split": False}
        
        tags = list(re.finditer(r'(\{\{page\|(.*?)\}\})', text, re.IGNORECASE))
        
        for match in tags:
            params = match.group(2)
            label = params.split('|')[0].strip()
            page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
            file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
            
            if page_check and file_check:
                pdf_num = int(page_check.group(1))
                filename = file_check.group(1).strip()
                
                if not master_pdf_filename:
                    master_pdf_filename = filename
                
                route_map[title]["pdf_pages"].append({
                    "pdf_num": pdf_num,
                    "label": label
                })
                
                old_text = extract_page_content(text, pdf_num)
                route_map[title]["old_texts"][pdf_num] = old_text

    # --- Resolve Duplicate Pages ---
    pdf_to_chapters = {}
    for ch in subpages:
        if ch not in route_map: continue
        for p in route_map[ch]["pdf_pages"]:
            pdf_num = p["pdf_num"]
            if pdf_num not in pdf_to_chapters:
                pdf_to_chapters[pdf_num] = []
            pdf_to_chapters[pdf_num].append(ch)
            
    for pdf_num, chapters in pdf_to_chapters.items():
        if len(chapters) > 1:
            # Sort by number of mapped pages ascending (standalone pages win over multi-page spans)
            chapters.sort(key=lambda c: len(route_map[c]["pdf_pages"]))
            winner = chapters[0]
            losers = chapters[1:]
            
            for loser in losers:
                # Remove from the route_map
                route_map[loser]["pdf_pages"] = [p for p in route_map[loser]["pdf_pages"] if p["pdf_num"] != pdf_num]
                route_map[loser]["old_texts"].pop(pdf_num, None)
                
                # Remove the {{page|...}} tag from local wikitext copy
                pattern = re.compile(rf'\{{\{{page\|[^}}]*?page\s*=\s*{pdf_num}[^}}]*\}}\}}\s*', re.IGNORECASE)
                wikitext_cache[loser] = pattern.sub('', wikitext_cache[loser])

    # --- Evaluate Split Logic AFTER Duplicate Resolution ---
    for ch in route_map:
        text = wikitext_cache.get(ch, "")
        stripped_text = text.lstrip()
        if stripped_text and not stripped_text.lower().startswith("{{page|"):
            route_map[ch]["needs_split"] = True

    # --- Re-sort subpages strictly by their lowest PDF page number ---
    sorted_subpages = []
    for sp in subpages:
        if sp not in route_map: continue
        pages = route_map[sp].get("pdf_pages", [])
        if pages:
            min_page = min(p["pdf_num"] for p in pages)
            sorted_subpages.append((min_page, sp))
        else:
            sorted_subpages.append((999999, sp)) # Push unmapped pages to the end

    sorted_subpages.sort(key=lambda x: x[0])
    ordered_subpages = [sp for _, sp in sorted_subpages]

    # --- Save a text file per chapter in the local PDF directory ---
    if master_pdf_filename:
        local_pdf_path = find_local_pdf(master_pdf_filename, input_folder)
        if local_pdf_path:
            pdf_dir = os.path.dirname(local_pdf_path)
            for sp in ordered_subpages:
                safe_sp = sp.replace("/", "_")
                ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                with open(ch_file_path, "w", encoding="utf-8") as f:
                    f.write(wikitext_cache.get(sp, ""))

    return route_map, master_pdf_filename, wikitext_cache, ordered_subpages

def find_local_pdf(filename, root_folder):
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower():
                return os.path.join(dirpath, f)
    return None

# ==============================================================================
# 3. UI & MAIN LOGIC
# ==============================================================================

st.title("📚 Book Re-Proofreader (Parallel Batch)")

st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio("OCR Strategy", ["Gemini (Default)", "DocAI Only"])

st.sidebar.divider()
st.sidebar.subheader("LLM Split Configuration")
split_prompt = st.sidebar.text_area(
    "Custom Split Instruction", 
    value="Look for the start of a new obituary. The person's name is usually in all caps or a distinct heading.",
    help="Context to help the LLM identify where a specific chapter begins."
)

st.sidebar.divider()

session = requests.Session()
try:
    get_csrf_token(session)
except Exception as e:
    st.error(f"Authentication Failed: {e}")
    st.stop()

# --- Master Queue Display ---
queue_data = load_queue()

if queue_data:
    with st.expander("📋 Processing Queue", expanded=False):
        df = [{"Book Title": title, "Status": data.get("status", "UNKNOWN")} for title, data in queue_data.items()]
        st.dataframe(df, use_container_width=True, hide_index=True)

all_books = list(queue_data.keys())
if not all_books:
    st.warning("Queue is empty. Check your queue JSON file.")
    st.stop()

st.divider()

# --- Select Any Book & Reset State ---
col1, col2 = st.columns([3, 1])
with col1:
    target_book = st.selectbox("Select Target Book (All Statuses Available)", all_books)
with col2:
    st.write("")
    st.write("")
    if st.button("🗑️ Reset Book State", use_container_width=True, help="Deletes the local map cache so you can re-process a completed book."):
        safe_title = target_book.replace("/", "_")
        state_file = os.path.join(CACHE_DIR, f"{safe_title}_state.json")
        
        # --- Clean up chapter .txt files ---
        state_to_delete = load_book_state(safe_title)
        master_pdf = state_to_delete.get("master_pdf")
        if master_pdf:
            local_pdf_path = find_local_pdf(master_pdf, input_folder)
            if local_pdf_path:
                pdf_dir = os.path.dirname(local_pdf_path)
                for sp in state_to_delete.get("subpages", []):
                    safe_sp = sp.replace("/", "_")
                    ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                    if os.path.exists(ch_file_path):
                        os.remove(ch_file_path)
                        
        if os.path.exists(state_file):
            os.remove(state_file)
        queue_data[target_book]["status"] = "PENDING"
        save_queue(queue_data)
        st.rerun()

safe_title = target_book.replace("/", "_")
state = load_book_state(safe_title)

# --- STEP 1: EXPLICIT AUTHORIZATION FOR ROUTE MAPPING ---
if not state.get("subpages"):
    st.info(f"Route map is missing or has been reset for **{target_book}**.")
    if st.button("🛠️ Generate Chapter Map", type="primary"):
        with st.spinner("🔍 Scanning wiki subpages and building route map..."):
            subpages = get_all_subpages(target_book, session)
            route_map, master_pdf_filename, wikitext_cache, ordered_subpages = build_sequential_route_map(subpages, session, input_folder)
            
            if not route_map or not master_pdf_filename:
                st.error(f"Could not find any {{page}} tags or PDF references for {target_book}.")
                st.stop()
                
            state["subpages"] = ordered_subpages
            state["route_map"] = route_map
            state["master_pdf"] = master_pdf_filename
            state["wikitext_cache"] = wikitext_cache
            save_book_state(safe_title, state)
            st.rerun()
    st.stop() # Halts all execution until authorized

# Display Map
st.subheader(f"📖 Map: {target_book}")
map_display = []
for sp in state["subpages"]:
    pdf_pages = state["route_map"].get(sp, {}).get("pdf_pages", [])
    page_labels = [str(p["pdf_num"]) for p in pdf_pages]
    needs_split = state["route_map"].get(sp, {}).get("needs_split", False)
    
    map_display.append({
        "Wiki Subpage": sp,
        "Mapped PDF Pages": ", ".join(page_labels) if page_labels else "None",
        "Needs Split Logic": "Yes" if needs_split else "No"
    })
st.dataframe(map_display, use_container_width=True, hide_index=True)

# --- STEP 2 & 3: AUTOMATED BATCH PROCESSING & WIKI UPLOAD ---
subpages_to_process = [sp for sp in state["subpages"] if sp not in state.get("completed_subpages", [])]
subpages_to_upload = [sp for sp in state["subpages"] if sp not in state.get("uploaded_subpages", [])]

# ==============================================================================
# STEP 3: WIKI UPLOAD PHASE (Runs when Offline is complete)
# ==============================================================================
if not subpages_to_process:
    st.success(f"✅ Offline processing complete for {target_book}! Local txt files are ready for review.")
    
    if not subpages_to_upload:
        st.success(f"🎉 All sections for {target_book} have been uploaded to the wiki!")
        queue_data[target_book]["status"] = "COMPLETED"
        save_queue(queue_data)
        st.stop()
        
    st.divider()
    st.subheader("Step 3: Wiki Upload Phase")
    st.info(f"Ready to upload {len(subpages_to_upload)} sections to the wiki.")
    
    if st.button("🌐 Upload All Chapters to Wiki", type="primary", use_container_width=True):
        progress_bar = st.progress(0)
        status_text = st.empty()
        log_container = st.container(border=True)
        
        local_pdf_path = find_local_pdf(state["master_pdf"], input_folder)
        pdf_dir = os.path.dirname(local_pdf_path) if local_pdf_path else ""
        
        if "uploaded_subpages" not in state:
            state["uploaded_subpages"] = []
            
        for idx, active_chapter in enumerate(subpages_to_upload):
            status_text.markdown(f"**Uploading {idx+1}/{len(subpages_to_upload)}:** `{active_chapter}`")
            progress_bar.progress(idx / len(subpages_to_upload))
            
            safe_sp = active_chapter.replace("/", "_")
            ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
            
            if not os.path.exists(ch_file_path):
                log_container.error(f"❌ Local file missing for {active_chapter}")
                continue
                
            with open(ch_file_path, "r", encoding="utf-8") as f:
                final_wikitext = f.read()
                
            log_container.write(f"🚀 Uploading {active_chapter}...")
            res = upload_to_bahaiworks(active_chapter, final_wikitext, "Bot: Parallel Batch Reproofread", session=session)
            
            if res.get('edit', {}).get('result') == 'Success':
                state["uploaded_subpages"].append(active_chapter)
                save_book_state(safe_title, state)
                log_container.write(f"✅ Success: {active_chapter}")
            else:
                log_container.error(f"❌ Upload failed: {res}")
                st.stop()
                
        progress_bar.progress(1.0)
        queue_data[target_book]["status"] = "COMPLETED"
        save_queue(queue_data)
        status_text.success("🎉 All chapters successfully uploaded to the wiki!")
        
    st.stop() # Halts execution here so the offline UI below is hidden


# ==============================================================================
# STEP 2: OFFLINE BATCH PROCESSING
# ==============================================================================
st.divider()
st.subheader("Step 2: Offline Batch Processing")
st.info(f"Ready to process {len(subpages_to_process)} remaining sections locally.")

col_start, col_stop = st.columns([1, 1])
with col_start:
    start_batch = st.button("🚀 Start Processing All Chapters", type="primary", use_container_width=True)
with col_stop:
    if st.button("🛑 Stop Execution", use_container_width=True):
        st.warning("Execution stopped.")
        st.stop()

if start_batch:
    progress_bar = st.progress(0)
    status_text = st.empty()
    log_container = st.container(border=True)
    
    # Initialize safe overflow cache
    if "overflow_cache" not in state:
        state["overflow_cache"] = {}
    
    local_pdf_path = find_local_pdf(state["master_pdf"], input_folder)
    if not local_pdf_path:
        st.error(f"Local PDF not found for '{state['master_pdf']}'")
        st.stop()
        
    pdf_dir = os.path.dirname(local_pdf_path)

    for idx, active_chapter in enumerate(subpages_to_process):
        active_chapter_idx = state["subpages"].index(active_chapter)
        next_chapter = state["subpages"][active_chapter_idx + 1] if active_chapter_idx + 1 < len(state["subpages"]) else None

        status_text.markdown(f"**Processing {idx+1}/{len(subpages_to_process)}:** `{active_chapter}`")
        progress_bar.progress(idx / len(subpages_to_process))
        
        log_container.write(f"--- Starting {active_chapter} ---")
        
        # --- Capture Year ---
        current_wikitext_for_year = state["wikitext_cache"].get(active_chapter, "")
        found_year = None
        cat_match = re.search(r'\[\[Category:\s*(\d{4})\s*\]\]', current_wikitext_for_year, re.IGNORECASE)
        if cat_match:
            found_year = cat_match.group(1)
        
        page_data = state["route_map"].get(active_chapter, {})
        pdf_targets = page_data.get("pdf_pages", [])
        
        # --- UNMAPPED CHAPTER HANDLING ---
        if not pdf_targets:
            current_wikitext = state["wikitext_cache"].get(active_chapter, "")
            overflow = state.get("overflow_cache", {}).get(active_chapter, "")
            
            # Combine safely bypassing any wiki-format strippers
            combined_text = f"{overflow}\n\n{current_wikitext}".strip()
            
            if combined_text:
                log_container.write("💾 Saving unmapped middle section locally...")
                
                # --- Apply cleanup and header formatting ---
                combined_text = cleanup_page_seams(combined_text)
                combined_text = apply_final_formatting(combined_text, active_chapter, found_year)
                
                safe_sp = active_chapter.replace("/", "_")
                ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                with open(ch_file_path, "w", encoding="utf-8") as f:
                    f.write(cleanup_page_seams(combined_text))
                
                state["completed_subpages"].append(active_chapter)
                save_book_state(safe_title, state)
                continue
            else:
                log_container.write("No PDF pages mapped and no cached text. Skipping section.")
                state["completed_subpages"].append(active_chapter)
                save_book_state(safe_title, state)
                continue

        pages_to_process = [t["pdf_num"] for t in pdf_targets]
        
        # --- MASTER JSON CHECK & TEXT EXTRACTION ---
        master_json_path = os.path.join(pdf_dir, f"master_{state['master_pdf']}.json")
        all_extracted_text = {}

        if os.path.exists(master_json_path):
            log_container.write(f"📄 Master JSON found. Reading text directly...")
            with open(master_json_path, 'r', encoding='utf-8') as f:
                master_data = json.load(f)
                for p_num in pages_to_process:
                    if str(p_num) in master_data:
                        all_extracted_text[int(p_num)] = master_data[str(p_num)]
                    else:
                        log_container.warning(f"⚠️ Page {p_num} missing from master JSON.")
        else:
            log_container.write("⚙️ Master JSON not found. Initiating batch OCR processing...")
            num_batches = 5
            batch_size = math.ceil(len(pages_to_process) / num_batches) if len(pages_to_process) > 0 else 1
            batches = [pages_to_process[j:j + batch_size] for j in range(0, len(pages_to_process), batch_size)]

            batch_placeholders = {}
            for i in range(len(batches)):
                if not batches[i]: continue
                start_pg, end_pg = batches[i][0], batches[i][-1]
                page_label = f"pg {start_pg}" if start_pg == end_pg else f"pgs {start_pg}-{end_pg}"
                with log_container.expander(f"Batch {i+1} Status ({page_label})", expanded=True):
                    batch_placeholders[i] = st.empty()

            from multiprocessing import Manager
            os.environ["PYTHONPATH"] = project_root

            with Manager() as manager:
                shared_logs = manager.dict()
                for i in range(len(batches)):
                    shared_logs[i] = manager.list()

                executor = concurrent.futures.ProcessPoolExecutor(max_workers=num_batches)
                futures = []
                for batch_id, batch_pages in enumerate(batches):
                    if not batch_pages: continue
                    future = executor.submit(
                        process_pdf_batch,
                        batch_id, batch_pages, local_pdf_path, ocr_strategy, 
                        state["master_pdf"], pdf_dir, shared_logs[batch_id]
                    )
                    futures.append(future)

                while True:
                    all_done = True
                    for batch_id, future in enumerate(futures):
                        current_logs = list(shared_logs.get(batch_id, []))
                        if current_logs and batch_id in batch_placeholders:
                            batch_placeholders[batch_id].text("\n".join(current_logs[-15:]))
                        if not future.done():
                            all_done = False
                    if all_done:
                        break
                    time.sleep(1)

                executor.shutdown(wait=False, cancel_futures=True)

            log_container.write("🔄 Assembling pages from batch temp files...")
            for batch_id in range(len(batches)):
                batch_file_path = os.path.join(pdf_dir, f"temp_{state['master_pdf']}_batch_{batch_id}.json")
                if os.path.exists(batch_file_path):
                    with open(batch_file_path, "r", encoding="utf-8") as f:
                        batch_data = json.load(f)
                        for p_num_str, text in batch_data.items():
                            all_extracted_text[int(p_num_str)] = text
                    os.remove(batch_file_path)

        # --- BRUTE FORCE SPLIT LOGIC ---
        if pages_to_process and next_chapter:
            last_page_num = pages_to_process[-1]
            last_page_text = all_extracted_text.get(last_page_num, "")
            
            if last_page_text:
                log_container.write(f"🧠 Asking LLM to check for chapter splits on page {last_page_num}...")
                
                ghost_chapters = [ch for ch in state["subpages"] if not state["route_map"].get(ch, {}).get("pdf_pages")]
                if active_chapter in ghost_chapters:
                    ghost_chapters.remove(active_chapter)
                    
                unmapped_to_pass = [ch for ch in ghost_chapters if ch != next_chapter]
                
                split_results = apply_chunked_split(last_page_text, next_chapter, unmapped_to_pass, split_prompt)
                
                if "_previous_" in split_results:
                    all_extracted_text[last_page_num] = split_results["_previous_"]
                    
                for found_chap, text_content in split_results.items():
                    if found_chap == "_previous_" or not text_content.strip(): 
                        continue
                    
                    # Store remainder safely in overflow cache
                    existing_overflow = state["overflow_cache"].get(found_chap, "")
                    marker = f""
                    
                    if marker not in existing_overflow:
                        state["overflow_cache"][found_chap] = f"{existing_overflow}\n{marker}\n{text_content}\n".strip()

        # Inject Current Chapter
        current_wikitext = state["wikitext_cache"].get(active_chapter, "")
        for target in pdf_targets:
            pdf_num = target["pdf_num"]
            label = target["label"]
            chunk_to_inject = all_extracted_text.get(pdf_num, "")
            
            if chunk_to_inject:
                current_wikitext, err = inject_text_into_page(current_wikitext, label, chunk_to_inject, state["master_pdf"])

        # Final Cleanup & Local Save (NO WIKI UPLOAD)
        final_wikitext = cleanup_page_seams(current_wikitext)
        
        # Pull any overflow belonging to this chapter and prepend it right before saving
        overflow = state.get("overflow_cache", {}).get(active_chapter, "")
        if overflow:
            final_wikitext = f"{overflow}\n\n{final_wikitext}"
            
        # --- Apply Header & OCR Cleanup ---
        final_wikitext = apply_final_formatting(final_wikitext, active_chapter, found_year)
            
        log_container.write(f"💾 Saving {active_chapter} to local txt file...")
        
        safe_sp = active_chapter.replace("/", "_")
        ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
        with open(ch_file_path, "w", encoding="utf-8") as f:
            f.write(final_wikitext)
            
        state["completed_subpages"].append(active_chapter)
        save_book_state(safe_title, state)

    # After loop finishes
    progress_bar.progress(1.0)
    st.success("✅ All chapters processed and saved locally!")
