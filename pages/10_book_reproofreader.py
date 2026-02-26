import streamlit as st
import os
import sys
import json
import re
import time
import requests
import fitz  # PyMuPDF
import math
import concurrent.futures
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
from src.batch_worker import process_pdf_batch, get_page_image_data
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

STATE_FILE = os.path.join(project_root, "book_sweeper_state.json")

st.set_page_config(page_title="Book Re-Proofreader", page_icon="📚", layout="wide")

# ==============================================================================
# 1. QUEUE & STATE MANAGEMENT
# ==============================================================================

def load_queue():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_queue(queue_data):
    with open(STATE_FILE, 'w') as f:
        json.dump(queue_data, f, indent=4)

def fetch_category_books(session):
    """Fetches all root pages in Category:Books."""
    books = []
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Books",
        "cmlimit": "500",
        "format": "json"
    }
    
    while True:
        try:
            res = session.get(API_URL, params=params).json()
            if 'error' in res:
                st.error(f"API Error: {res['error']}")
                break
            
            chunk = res.get('query', {}).get('categorymembers', [])
            books.extend([m['title'] for m in chunk if not m['title'].startswith('Category:')])
            
            if 'continue' in res:
                params.update(res['continue'])
            else:
                break
        except Exception as e:
            st.error(f"Network Error: {e}")
            break
            
    return books

def sync_queue(session):
    """Syncs the local state with the live category."""
    current_queue = load_queue()
    live_books = fetch_category_books(session)
    
    added = 0
    for book in live_books:
        if book not in current_queue:
            current_queue[book] = {"status": "PENDING", "last_updated": time.time()}
            added += 1
            
    save_queue(current_queue)
    return added, current_queue

# ==============================================================================
# 2. WIKI & ROUTE MAP HELPERS
# ==============================================================================

def get_all_subpages(root_title, session):
    """Finds all subpages for a book, including the root page itself."""
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
            
    return pages

def extract_page_content(wikitext, pdf_page_num):
    """Extracts the existing text content inside the {{page|...}} tag for splitting calculations."""
    tags = re.finditer(r'(\{\{page\|(.*?)\}\})(.*?)(?=\{\{page\||\Z)', wikitext, re.IGNORECASE | re.DOTALL)
    for match in tags:
        params = match.group(2)
        content = match.group(3).strip()
        
        page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
        if page_check and int(page_check.group(1)) == pdf_page_num:
            # Strip trailing {{ocr}} if present in the content block
            content = re.sub(r'\{\{ocr\}\}\s*\Z', '', content, flags=re.IGNORECASE).strip()
            return content
    return ""

def build_route_map(subpages, session):
    """
    Builds a dictionary mapping physical PDF pages to the wiki subpages they appear on.
    Returns: map, pdf_filename, wikitext_cache
    """
    route_map = {}
    wikitext_cache = {}
    master_pdf_filename = None
    
    for title in subpages:
        text, err = fetch_wikitext(title, session=session)
        if err or not text:
            continue
            
        wikitext_cache[title] = text
        
        # Find all page tags on this subpage
        tags = re.finditer(r'\{\{page\|(.*?)\}\}', text, re.IGNORECASE | re.DOTALL)
        for match in tags:
            params = match.group(1)
            
            label = params.split('|')[0].strip()
            page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
            file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
            
            if page_check and file_check:
                pdf_num = int(page_check.group(1))
                filename = file_check.group(1).strip()
                
                if not master_pdf_filename:
                    master_pdf_filename = filename
                    
                if pdf_num not in route_map:
                    route_map[pdf_num] = []
                    
                old_text = extract_page_content(text, pdf_num)
                
                route_map[pdf_num].append({
                    "subpage": title,
                    "label": label,
                    "old_text": old_text
                })
                
    return route_map, master_pdf_filename, wikitext_cache

def slice_text_for_split_pages(new_text, old_texts):
    """
    Slices newly generated AI text proportionally based on the lengths of the old text segments.
    """
    if not new_text:
        return [""] * len(old_texts)
    if len(old_texts) <= 1:
        return [new_text]

    total_old_len = sum(len(t) for t in old_texts)
    if total_old_len == 0:
        # Fallback: equal split
        avg = len(new_text) // len(old_texts)
        return [new_text[i*avg:(i+1)*avg] for i in range(len(old_texts))]

    slices = []
    current_idx = 0
    
    for i in range(len(old_texts) - 1):
        target_len = int(len(new_text) * (len(old_texts[i]) / total_old_len))
        split_point = current_idx + target_len

        # Try to snap to the nearest newline to avoid breaking sentences
        nearest_nl = new_text.find('\n', max(current_idx, split_point - 100), min(len(new_text), split_point + 100))
        if nearest_nl != -1:
            split_point = nearest_nl + 1
        else:
            # Fallback to nearest space
            nearest_space = new_text.find(' ', max(current_idx, split_point - 50), min(len(new_text), split_point + 50))
            if nearest_space != -1:
                split_point = nearest_space + 1

        slices.append(new_text[current_idx:split_point].strip())
        current_idx = split_point

    # Last slice gets the remainder
    slices.append(new_text[current_idx:].strip())
    return slices

def find_local_pdf(filename, root_folder):
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower():
                return os.path.join(dirpath, f)
    return None

# ==============================================================================
# 3. UI & MAIN LOGIC
# ==============================================================================

st.title("📚 Book Re-Proofreader (Cross-Chapter)")
st.markdown("Re-processes existing books mapped via `Category:Books`, handling text that splits across chapters seamlessly.")

st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio("OCR Strategy", ["Gemini (Default)", "DocAI Only"])
run_mode = st.sidebar.radio("Run Mode", ["Test (1 Book)", "Production (Continuous)"])

st.sidebar.divider()

session = requests.Session()
try:
    with st.spinner("Authenticating with MediaWiki..."):
        get_csrf_token(session)
except Exception as e:
    st.error(f"Authentication Failed: {e}")
    st.stop()

if st.sidebar.button("🔄 Sync Category:Books"):
    with st.spinner("Fetching category list..."):
        added, _ = sync_queue(session)
        st.sidebar.success(f"Synced! Added {added} new books to queue.")
        time.sleep(1)
        st.rerun()

queue_data = load_queue()

if not queue_data:
    st.warning("Queue is empty. Click 'Sync Category:Books' in the sidebar to populate it.")
    st.stop()

# Build Queue DataFrame
df = []
for title, data in queue_data.items():
    df.append({"Book Title": title, "Status": data.get("status", "UNKNOWN")})

st.subheader("📋 Processing Queue")
st.dataframe(df, use_container_width=True, hide_index=True)

# Determine Target
pending_books = [t for t, d in queue_data.items() if d.get("status") in ["PENDING", "ERROR"]]

col1, col2 = st.columns([2, 1])
with col1:
    target_book = st.selectbox("Select Target Book (Test Mode)", ["--- Auto Select Next ---"] + pending_books)
with col2:
    st.write("") # Spacer
    st.write("")
    start_btn = st.button("🚀 Start Processing", type="primary", use_container_width=True)

if start_btn:
    if not pending_books:
        st.success("All books in queue are completed!")
        st.stop()

    book_list = [target_book] if target_book != "--- Auto Select Next ---" else pending_books
    
    if run_mode.startswith("Test"):
        book_list = book_list[:1]

    for book_title in book_list:
        st.markdown(f"### ⚙️ Processing: `{book_title}`")
        log_area = st.empty()
        
        # 1. Subpage Discovery
        log_area.text("🔍 Discovering subpages...")
        subpages = get_all_subpages(book_title, session)
        
        # 2. Build Route Map
        log_area.text(f"🗺️ Building cross-chapter route map across {len(subpages)} wiki pages...")
        route_map, master_pdf_filename, wikitext_cache = build_route_map(subpages, session)
        
        if not route_map or not master_pdf_filename:
            st.error(f"Could not find any {{page}} tags or PDF references for {book_title}.")
            queue_data[book_title]["status"] = "ERROR"
            save_queue(queue_data)
            continue
            
        # 3. Locate PDF
        local_pdf_path = find_local_pdf(master_pdf_filename, input_folder)
        if not local_pdf_path:
            st.error(f"Local PDF not found for '{master_pdf_filename}'")
            queue_data[book_title]["status"] = "ERROR"
            save_queue(queue_data)
            continue
            
        unique_pdf_pages = sorted(list(route_map.keys()))
        log_area.text(f"🚀 Found {len(unique_pdf_pages)} unique physical pages to process.")

        # 4. Parallel Batch OCR
        num_batches = 5
        batch_size = math.ceil(len(unique_pdf_pages) / num_batches)
        batches = [unique_pdf_pages[j:j + batch_size] for j in range(0, len(unique_pdf_pages), batch_size)]
        
        all_extracted_text = {}
        
        from multiprocessing import Manager
        with Manager() as manager:
            shared_logs = manager.dict()
            for i in range(len(batches)):
                shared_logs[i] = manager.list()
                
            with st.spinner("Running AI Document Extraction..."):
                executor = concurrent.futures.ProcessPoolExecutor(max_workers=num_batches)
                futures = []
                for batch_id, batch_pages in enumerate(batches):
                    future = executor.submit(
                        process_pdf_batch,
                        batch_id, batch_pages, local_pdf_path, ocr_strategy, 
                        master_pdf_filename, project_root, shared_logs[batch_id]
                    )
                    futures.append(future)

                # Polling loop omitted for brevity, waiting for completion
                concurrent.futures.wait(futures)
                executor.shutdown(wait=False, cancel_futures=True)

        # 5. Load Results
        for batch_id in range(len(batches)):
            batch_file = os.path.join(project_root, f"temp_{master_pdf_filename}_batch_{batch_id}.json")
            if os.path.exists(batch_file):
                with open(batch_file, "r", encoding="utf-8") as f:
                    batch_data = json.load(f)
                    for p_num_str, text in batch_data.items():
                        all_extracted_text[int(p_num_str)] = text
                os.remove(batch_file)

        # 6. Slice and Assign Text to Subpages
        log_area.text("✂️ Slicing text for split chapters...")
        injection_queue = {sp: wikitext_cache[sp] for sp in subpages}

        for pdf_page in unique_pdf_pages:
            new_text = all_extracted_text.get(pdf_page, "")
            if not new_text or "GEMINI_ERROR" in new_text or "DOCAI_ERROR" in new_text:
                st.warning(f"Failed extraction on page {pdf_page}. Skipping.")
                continue

            targets = route_map[pdf_page]
            old_texts = [t["old_text"] for t in targets]
            
            # Slice the new text based on the lengths of the old texts
            sliced_texts = slice_text_for_split_pages(new_text, old_texts)

            for idx, target in enumerate(targets):
                subpage_title = target["subpage"]
                label = target["label"]
                chunk = sliced_texts[idx]

                # Update the working wikitext in memory
                updated_text, err = inject_text_into_page(
                    injection_queue[subpage_title], 
                    label, 
                    chunk, 
                    master_pdf_filename
                )
                if not err:
                    injection_queue[subpage_title] = updated_text

        # 7. Upload Subpages Sequentially
        log_area.text("☁️ Uploading updated subpages...")
        upload_progress = st.progress(0)
        
        for idx, subpage_title in enumerate(subpages):
            final_wikitext = cleanup_page_seams(injection_queue[subpage_title])
            
            res = upload_to_bahaiworks(
                subpage_title, 
                final_wikitext, 
                "Bot: Cross-chapter book reproofread", 
                session=session
            )
            
            if res.get('edit', {}).get('result') != 'Success':
                st.error(f"Upload failed for {subpage_title}: {res}")
            
            upload_progress.progress((idx + 1) / len(subpages))
            time.sleep(1) # Rate limit protection

        # 8. Mark Complete
        queue_data[book_title]["status"] = "COMPLETED"
        queue_data[book_title]["last_updated"] = time.time()
        save_queue(queue_data)
        st.success(f"✅ Finished: {book_title}")

    st.balloons()
