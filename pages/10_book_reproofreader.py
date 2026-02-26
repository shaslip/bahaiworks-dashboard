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
        # Relies on find_local_pdf already being defined in the file
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
            # Unpack the new ordered_subpages list
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

# --- STEP 2: PROCESS ENTIRE PDF ---
st.divider()
st.subheader("Action Required")

master_pdf = state.get("master_pdf")
if not master_pdf:
    st.warning("Generate Chapter Map first to identify the master PDF.")
    st.stop()

local_pdf_path = find_local_pdf(master_pdf, input_folder)
if not local_pdf_path:
    st.error(f"Local PDF not found for '{master_pdf}'")
    st.stop()

pdf_dir = os.path.dirname(local_pdf_path)
master_json_path = os.path.join(pdf_dir, f"master_{master_pdf}.json")

if os.path.exists(master_json_path):
    st.success(f"✅ Full book processing already completed! Master record found at {master_json_path}")
    st.info("The next step (splitting and uploading) will be built here soon.")
    st.stop()

st.info(f"Next task: Run **{ocr_strategy}** on the entire book (`{master_pdf}`).")

col_start, col_stop = st.columns([1, 1])
with col_start:
    if st.button(f"🚀 Run {ocr_strategy}", type="primary", use_container_width=True):
        st.session_state['processing_active'] = "FULL_BOOK"
        st.rerun()
with col_stop:
    if st.button("🛑 Stop Execution", use_container_width=True):
        st.session_state.pop('processing_active', None)
        st.warning("Execution stopped.")
        st.stop()

# Execution bound to session state so it doesn't vanish
if st.session_state.get('processing_active') == "FULL_BOOK":
    
    log_container = st.container(border=True)
    log_container.write(f"Starting parallel processing for the entire book: {master_pdf}...")
    
    doc = fitz.open(local_pdf_path)
    total_pages = len(doc)
    doc.close()

    pages_to_process = list(range(1, total_pages + 1))
    num_batches = 10
    batch_size = math.ceil(len(pages_to_process) / num_batches)
    batches = [pages_to_process[j:j + batch_size] for j in range(0, len(pages_to_process), batch_size)]

    st.write(f"🚀 Split {total_pages} pages across {len(batches)} batches.")

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
                master_pdf, pdf_dir, shared_logs[batch_id]
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

        # Force shutdown to prevent UI hang
        executor.shutdown(wait=False, cancel_futures=True)
        
        # Ruthlessly kill the background workers so gRPC threads don't cause a RAM leak
        import signal
        if hasattr(executor, '_processes') and executor._processes is not None:
            for pid in executor._processes.keys():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    # --- OFFLINE ASSEMBLY / MERGE MASTER RECORD ---
    log_container.write("🔄 Merging all batches into master record...")
    master_data = {}
    
    for batch_id in range(len(batches)):
        batch_file_path = os.path.join(pdf_dir, f"temp_{master_pdf}_batch_{batch_id}.json")
        if os.path.exists(batch_file_path):
            with open(batch_file_path, "r", encoding="utf-8") as f:
                batch_data = json.load(f)
                for p_num_str, text in batch_data.items():
                    master_data[int(p_num_str)] = text

    # Save sorted master record
    sorted_master = {str(k): master_data[k] for k in sorted(master_data.keys())}
    with open(master_json_path, "w", encoding="utf-8") as f:
        json.dump(sorted_master, f, indent=4)

    # Cleanup temp batches
    for batch_id in range(len(batches)):
        batch_file_path = os.path.join(pdf_dir, f"temp_{master_pdf}_batch_{batch_id}.json")
        if os.path.exists(batch_file_path):
            os.remove(batch_file_path)

    st.session_state.pop('processing_active', None)
    st.success(f"✅ Full book processing complete! Master record saved to {master_json_path}")
    time.sleep(1)
    st.rerun()
