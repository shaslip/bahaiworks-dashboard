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
    cleanup_page_seams
)

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

def build_sequential_route_map(subpages, session):
    route_map = {}
    master_pdf_filename = None
    wikitext_cache = {}
    
    for title in subpages:
        text, err = fetch_wikitext(title, session=session)
        if err or not text:
            continue
            
        wikitext_cache[title] = text
        route_map[title] = {"pdf_pages": [], "old_texts": {}}
        
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

    # --- Re-sort subpages strictly by their lowest PDF page number ---
    sorted_subpages = []
    for sp in subpages:
        pages = route_map.get(sp, {}).get("pdf_pages", [])
        if pages:
            min_page = min(p["pdf_num"] for p in pages)
            sorted_subpages.append((min_page, sp))
        else:
            sorted_subpages.append((999999, sp)) # Push unmapped pages to the end

    sorted_subpages.sort(key=lambda x: x[0])
    ordered_subpages = [sp for _, sp in sorted_subpages]

    return route_map, master_pdf_filename, wikitext_cache, ordered_subpages

def find_local_pdf(filename, root_folder):
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower():
                return os.path.join(dirpath, f)
    return None

def apply_proportional_split(full_ai_text, old_part1, old_part2):
    """
    Splits the AI text proportionally without difflib matching. 
    Prevents typo/formatting corrections from throwing off the split logic.
    """
    if not old_part1 or not old_part2:
        return full_ai_text, ""
        
    ratio = len(old_part1) / float(len(old_part1) + len(old_part2) + 1)
    target_idx = int(len(full_ai_text) * ratio)
    
    # Try to snap to a clean paragraph break
    breaks = [m.start() for m in re.finditer(r'\n+', full_ai_text)]
    if not breaks:
        breaks = [m.start() for m in re.finditer(r'\s+', full_ai_text)]
        
    if breaks:
        closest_break = min(breaks, key=lambda x: abs(x - target_idx))
        return full_ai_text[:closest_break].strip(), full_ai_text[closest_break:].strip()
        
    return full_ai_text, ""

# ==============================================================================
# 3. UI & MAIN LOGIC
# ==============================================================================

st.title("📚 Book Re-Proofreader (Parallel Batch)")

st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio("OCR Strategy", ["Gemini (Default)", "DocAI Only"])

st.sidebar.divider()

session = requests.Session()
try:
    get_csrf_token(session)
except Exception as e:
    st.error(f"Authentication Failed: {e}")
    st.stop()

# --- RESTORED: Master Queue Display ---
queue_data = load_queue()

if queue_data:
    df = [{"Book Title": title, "Status": data.get("status", "UNKNOWN")} for title, data in queue_data.items()]
    st.subheader("📋 Processing Queue")
    st.dataframe(df, use_container_width=True, hide_index=True)

all_books = list(queue_data.keys())
if not all_books:
    st.warning("Queue is empty. Check your queue JSON file.")
    st.stop()

st.divider()

# --- UNLOCKED: Select Any Book & Reset State ---
col1, col2 = st.columns([3, 1])
with col1:
    target_book = st.selectbox("Select Target Book (All Statuses Available)", all_books)
with col2:
    st.write("")
    st.write("")
    if st.button("🗑️ Reset Book State", use_container_width=True, help="Deletes the local map cache so you can re-process a completed book."):
        safe_title = target_book.replace("/", "_")
        state_file = os.path.join(CACHE_DIR, f"{safe_title}_state.json")
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
            # Unpack the new ordered_subpages list
            route_map, master_pdf_filename, wikitext_cache, ordered_subpages = build_sequential_route_map(subpages, session)
            
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
    map_display.append({
        "Wiki Subpage": sp,
        "Mapped PDF Pages": ", ".join(page_labels) if page_labels else "None"
    })
st.dataframe(map_display, use_container_width=True, hide_index=True)

# --- STEP 2: FIND NEXT TASK & PAUSE ---
subpages_to_process = [sp for sp in state["subpages"] if sp not in state.get("completed_subpages", [])]

if not subpages_to_process:
    st.success(f"✅ All sections for {target_book} completed!")
    queue_data[target_book]["status"] = "COMPLETED"
    save_queue(queue_data)
    st.stop()

active_chapter = subpages_to_process[0]
active_chapter_idx = state["subpages"].index(active_chapter)
next_chapter = state["subpages"][active_chapter_idx + 1] if active_chapter_idx + 1 < len(state["subpages"]) else None

st.divider()
st.subheader("Action Required")
st.info(f"Next task: Processing section **{active_chapter}**")

col_start, col_stop = st.columns([1, 1])
with col_start:
    if st.button("🚀 Yes, Process This Section", type="primary", use_container_width=True):
        st.session_state['processing_active'] = active_chapter
        st.rerun()
with col_stop:
    if st.button("🛑 Stop Execution", use_container_width=True):
        st.session_state.pop('processing_active', None)
        st.warning("Execution stopped.")
        st.stop()

# Execution bound to session state so it doesn't vanish
if st.session_state.get('processing_active') == active_chapter:
    
    log_container = st.container(border=True)
    log_container.write(f"Starting parallel processing for {active_chapter}...")
    
    local_pdf_path = find_local_pdf(state["master_pdf"], input_folder)
    if not local_pdf_path:
        st.error(f"Local PDF not found for '{state['master_pdf']}'")
        st.session_state.pop('processing_active', None)
        st.stop()

    page_data = state["route_map"].get(active_chapter, {})
    pdf_targets = page_data.get("pdf_pages", [])
    
    if not pdf_targets:
        log_container.write("No PDF pages mapped. Skipping section.")
        state["completed_subpages"].append(active_chapter)
        save_book_state(safe_title, state)
        st.session_state.pop('processing_active', None)
        st.rerun()

    pages_to_process = [t["pdf_num"] for t in pdf_targets]
    
    # --- BATCH PREP ---
    num_batches = 5
    batch_size = math.ceil(len(pages_to_process) / num_batches)
    batches = [pages_to_process[j:j + batch_size] for j in range(0, len(pages_to_process), batch_size)]

    batch_placeholders = {}
    for i in range(len(batches)):
        if not batches[i]: continue
        start_pg, end_pg = batches[i][0], batches[i][-1]
        page_label = f"pg {start_pg}" if start_pg == end_pg else f"pgs {start_pg}-{end_pg}"
        with log_container.expander(f"Batch {i+1} Status ({page_label})", expanded=True):
            batch_placeholders[i] = st.empty()

    # --- PARALLEL EXECUTION ---
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
                state["master_pdf"], project_root, shared_logs[batch_id]
            )
            futures.append(future)

        # Polling Loop
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

    # --- OFFLINE ASSEMBLY & SPLIT LOGIC ---
    log_container.write("🔄 Assembling pages and applying split logic...")
    all_extracted_text = {}
    for batch_id in range(len(batches)):
        batch_file_path = os.path.join(project_root, f"temp_{state['master_pdf']}_batch_{batch_id}.json")
        if os.path.exists(batch_file_path):
            with open(batch_file_path, "r", encoding="utf-8") as f:
                batch_data = json.load(f)
                for p_num_str, text in batch_data.items():
                    all_extracted_text[int(p_num_str)] = text
            os.remove(batch_file_path)

    current_wikitext = state["wikitext_cache"].get(active_chapter, "")

    last_page_num = pages_to_process[-1]
    last_page_ai_text = all_extracted_text.get(last_page_num, "")

    if next_chapter:
        next_pages = state["route_map"].get(next_chapter, {}).get("pdf_pages", [])
        if next_pages and next_pages[0]["pdf_num"] == last_page_num:
            log_container.write(f"✂️ Split boundary detected on Page {last_page_num} with next section.")
            
            old_curr_chapter_snippet = page_data["old_texts"].get(last_page_num, "")
            old_next_chapter_snippet = state["route_map"][next_chapter]["old_texts"].get(last_page_num, "")
            
            part_current, part_next = apply_proportional_split(last_page_ai_text, old_curr_chapter_snippet, old_next_chapter_snippet)
            
            all_extracted_text[last_page_num] = part_current
            
            next_wikitext = state["wikitext_cache"].get(next_chapter, "")
            state["route_map"][next_chapter]["old_texts"][last_page_num] = part_next
            
            next_wikitext = re.sub(rf'\{{\{{page\|.*?page={last_page_num}\}}\}}\s*\{{\{{ocr.*?\}}\}}?\n?', '', next_wikitext, flags=re.IGNORECASE)
            
            state["wikitext_cache"][next_chapter] = part_next + "\n\n" + next_wikitext.lstrip()

    # Inject Current Chapter
    for target in pdf_targets:
        pdf_num = target["pdf_num"]
        label = target["label"]
        chunk_to_inject = all_extracted_text.get(pdf_num, "")
        
        if chunk_to_inject:
            current_wikitext, err = inject_text_into_page(current_wikitext, label, chunk_to_inject, state["master_pdf"])

    # Final Cleanup & Upload
    final_wikitext = cleanup_page_seams(current_wikitext)
    log_container.write("🚀 Uploading section to wiki...")
    
    res = upload_to_bahaiworks(active_chapter, final_wikitext, "Bot: Parallel Batch Reproofread", session=session)
    
    if res.get('edit', {}).get('result') == 'Success':
        state["completed_subpages"].append(active_chapter)
        save_book_state(safe_title, state)
        st.session_state.pop('processing_active', None)
        st.success(f"Upload complete for {active_chapter}!")
        time.sleep(1)
        st.rerun()
    else:
        st.error(f"❌ Upload failed: {res}")
        st.session_state.pop('processing_active', None)
