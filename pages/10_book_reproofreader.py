import streamlit as st
import os
import sys
import json
import re
import time
import requests
import difflib
import math

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
    get_csrf_token,
    cleanup_page_seams
)

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

QUEUE_FILE = os.path.join(project_root, "book_sweeper_queue.json")
CACHE_DIR = os.path.join(project_root, "book_cache")

if not os.path.exists(CACHE_DIR):
    os.makedirs(CACHE_DIR)

# --- Human-readable offline proofs ---
OFFLINE_DIR = os.path.join(project_root, "offline_proofs")
if not os.path.exists(OFFLINE_DIR):
    os.makedirs(OFFLINE_DIR)

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

def load_extracted_cache(safe_title):
    cache_file = os.path.join(CACHE_DIR, f"{safe_title}_extracted.json")
    if os.path.exists(cache_file):
        with open(cache_file, 'r') as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}

def save_extracted_cache(safe_title, cache_data):
    cache_file = os.path.join(CACHE_DIR, f"{safe_title}_extracted.json")
    with open(cache_file, 'w') as f:
        json.dump(cache_data, f, indent=4)

def fetch_category_books(session):
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
    
    current_pdf_page = None
    current_label = None
    
    # --- NEW: Track globally seen pages to catch duplicates ---
    seen_pdf_pages = set()

    for title in subpages:
        text, err = fetch_wikitext(title, session=session)
        if err or not text:
            continue
            
        wikitext_cache[title] = text
        route_map[title] = {"pdf_pages": [], "old_texts": {}}
        
        # --- CHANGED: Capture full block including optional {{ocr}} for clean deletion ---
        tags = list(re.finditer(r'(\{\{page\|(.*?)\}\})(\s*\{\{ocr\}\})?', text, re.IGNORECASE | re.DOTALL))
        
        if not tags and current_pdf_page is not None:
            # Inherits the physical page from the previous subpage
            route_map[title]["pdf_pages"].append({
                "pdf_num": current_pdf_page,
                "label": current_label,
                "inherited": True
            })
            route_map[title]["old_texts"][current_pdf_page] = text.strip()
            continue
            
        for match in tags:
            full_tag_block = match.group(0)
            params = match.group(2)
            
            label = params.split('|')[0].strip()
            page_check = re.search(r'page\s*=\s*(\d+)', params, re.IGNORECASE)
            file_check = re.search(r'file\s*=\s*([^|}\n]+)', params, re.IGNORECASE)
            
            if page_check and file_check:
                pdf_num = int(page_check.group(1))
                filename = file_check.group(1).strip()
                
                if not master_pdf_filename:
                    master_pdf_filename = filename
                    
                # --- Duplicate Tag Removal Logic ---
                if pdf_num in seen_pdf_pages:
                    # Strip the duplicate tag from the text entirely
                    text = text.replace(full_tag_block, "")
                    wikitext_cache[title] = text
                    
                    # Treat it as inherited overflow from the previous page
                    route_map[title]["pdf_pages"].append({
                        "pdf_num": pdf_num,
                        "label": label,
                        "inherited": True
                    })
                    
                    # Re-extract what's left as the old text
                    route_map[title]["old_texts"][pdf_num] = text.strip()
                    continue
                
                seen_pdf_pages.add(pdf_num)
                current_pdf_page = pdf_num
                current_label = label
                
                route_map[title]["pdf_pages"].append({
                    "pdf_num": pdf_num,
                    "label": label,
                    "inherited": False
                })
                
                old_text = extract_page_content(text, pdf_num)
                route_map[title]["old_texts"][pdf_num] = old_text

    return route_map, master_pdf_filename, wikitext_cache

def fuzzy_slice(ai_text, old_text_snippet):
    """
    Attempts to find the boundaries of the old_text_snippet within the new ai_text.
    Uses difflib to find the best matching block.
    """
    if not ai_text or not old_text_snippet:
        return ""
    
    # Clean up for comparison
    clean_ai = re.sub(r'\s+', ' ', ai_text).strip()
    clean_old = re.sub(r'\s+', ' ', old_text_snippet).strip()
    
    matcher = difflib.SequenceMatcher(None, clean_ai, clean_old)
    match = matcher.find_longest_match(0, len(clean_ai), 0, len(clean_old))
    
    if match.size > 0:
        # We found a match in the cleaned string. We need to map this back to the original ai_text with newlines.
        # This is an approximation. A robust implementation would map indices exactly.
        # For safety, if fuzzy fails, we return the ai_text and let the injection logic handle it if possible, 
        # or rely on proportional slicing as a fallback.
        
        # Simple proportional fallback if difflib gets too complex with formatting:
        start_ratio = match.a / len(clean_ai)
        end_ratio = (match.a + match.size) / len(clean_ai)
        
        start_idx = int(start_ratio * len(ai_text))
        end_idx = int(end_ratio * len(ai_text))
        
        # Snap to nearest word boundaries
        while start_idx > 0 and ai_text[start_idx-1] not in [' ', '\n']:
            start_idx -= 1
        while end_idx < len(ai_text) and ai_text[end_idx] not in [' ', '\n']:
            end_idx += 1
            
        return ai_text[start_idx:end_idx].strip()
        
    return ai_text

def get_page_image_local(pdf_path, page_num):
    from PIL import Image
    import io
    try:
        doc = fitz.open(pdf_path)
        if page_num > len(doc) or page_num < 1:
            doc.close()
            return None
        page = doc.load_page(page_num - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        doc.close()
        return img
    except Exception:
        return None

def find_local_pdf(filename, root_folder):
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower():
                return os.path.join(dirpath, f)
    return None

# ==============================================================================
# 3. UI & MAIN LOGIC
# ==============================================================================

st.title("📚 Book Re-Proofreader (Sequential)")
st.markdown("Sequentially processes book subpages, handling split-page content seamlessly with local caching.")

st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio("OCR Strategy", ["Gemini (Default)", "DocAI Only"])
run_mode = st.sidebar.radio("Run Mode", ["Test (1 Book)", "Production (Continuous)"])

st.sidebar.divider()

session = requests.Session()
try:
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

pending_books = [t for t, d in queue_data.items() if d.get("status") in ["PENDING", "ERROR"]]

# --- TARGET SELECTION ---
target_book = st.selectbox("Select Target Book", ["--- Select a Book ---"] + pending_books)

if target_book != "--- Select a Book ---":
    safe_title = target_book.replace("/", "_")
    state = load_book_state(safe_title)
    
    st.divider()
    
    # --- STEP 1: ROUTE MAPPING & DISPLAY ---
    st.subheader(f"📖 Chapter Map: {target_book}")
    
    if not state.get("subpages"):
        if st.button("🔍 Step 1: Map Chapters & Pages", type="primary"):
            with st.spinner("Scanning wiki subpages and physical PDF pages..."):
                subpages = get_all_subpages(target_book, session)
                route_map, master_pdf_filename, wikitext_cache = build_sequential_route_map(subpages, session)
                
                if not route_map or not master_pdf_filename:
                    st.error(f"Could not find any {{page}} tags or PDF references for {target_book}.")
                    queue_data[target_book]["status"] = "ERROR"
                    save_queue(queue_data)
                    st.stop()
                    
                state["subpages"] = subpages
                state["route_map"] = route_map
                state["master_pdf"] = master_pdf_filename
                state["wikitext_cache"] = wikitext_cache
                save_book_state(safe_title, state)
                st.rerun()
    else:
        # Display the ordered list to the user
        map_display = []
        for sp in state["subpages"]:
            pdf_pages = state["route_map"].get(sp, {}).get("pdf_pages", [])
            page_labels = [str(p["pdf_num"]) + (" (Inherited)" if p["inherited"] else "") for p in pdf_pages]
            status = "✅ Done" if sp in state.get("completed_subpages", []) else "⏳ Pending"
            map_display.append({
                "Wiki Subpage": sp,
                "Mapped PDF Pages": ", ".join(page_labels) if page_labels else "None",
                "Status": status
            })
            
        st.dataframe(map_display, use_container_width=True, hide_index=True)

        # --- STEP 2: SEQUENTIAL EXECUTION ---
        st.divider()
        col1, col2 = st.columns([1, 1])
        with col1:
            start_btn = st.button("🚀 Step 2: Start Processing Chapters", type="primary", use_container_width=True)
        with col2:
            stop_btn = st.button("🛑 Stop Process", use_container_width=True)

        if start_btn:
            st.session_state['running_book'] = True

        if 'running_book' in st.session_state and st.session_state['running_book']:
            
            master_pdf_filename = state["master_pdf"]
            local_pdf_path = find_local_pdf(master_pdf_filename, input_folder)
            
            if not local_pdf_path:
                st.error(f"Local PDF not found for '{master_pdf_filename}'")
                queue_data[target_book]["status"] = "ERROR"
                save_queue(queue_data)
                st.session_state.pop('running_book', None)
                st.stop()

            extracted_cache = load_extracted_cache(safe_title)
            subpages_to_process = [sp for sp in state["subpages"] if sp not in state.get("completed_subpages", [])]
            
            if not subpages_to_process:
                st.success("✅ All subpages already processed.")
                queue_data[target_book]["status"] = "COMPLETED"
                save_queue(queue_data)
                st.session_state.pop('running_book', None)
            else:
                status_box = st.container(border=True)
                log_area = status_box.empty()
                progress_bar = status_box.progress(0)
                
                for idx, subpage_title in enumerate(subpages_to_process):
                    
                    if stop_btn:
                        st.warning("🛑 Stop requested! Progress saved.")
                        st.session_state.pop('running_book', None)
                        st.stop()
                        
                    log_area.text(f"📝 Processing Subpage ({idx+1}/{len(subpages_to_process)}): {subpage_title}")
                    
                    page_data = state["route_map"].get(subpage_title, {})
                    pdf_targets = page_data.get("pdf_pages", [])
                    
                    if not pdf_targets:
                        log_area.text(f"⏭️ No PDF pages mapped to {subpage_title}. Skipping.")
                        if "completed_subpages" not in state: state["completed_subpages"] = []
                        state["completed_subpages"].append(subpage_title)
                        save_book_state(safe_title, state)
                        continue

                    current_wikitext, _ = fetch_wikitext(subpage_title, session=session)
                    if not current_wikitext:
                        current_wikitext = state["wikitext_cache"].get(subpage_title, "")

                    for target in pdf_targets:
                        pdf_num = target["pdf_num"]
                        label = target["label"]
                        is_inherited = target["inherited"]
                        
                        if pdf_num not in extracted_cache:
                            log_area.text(f"   ➔ OCR Extraction for PDF Page {pdf_num}...")
                            img = get_page_image_local(local_pdf_path, pdf_num)
                            
                            if img:
                                if ocr_strategy == "DocAI Only":
                                    raw_ocr = transcribe_with_document_ai(img)
                                    new_text = reformat_raw_text(raw_ocr) if raw_ocr else ""
                                else:
                                    new_text = proofread_with_formatting(img)
                                    if "GEMINI_ERROR" in new_text:
                                        new_text = reformat_raw_text(transcribe_with_document_ai(img))
                                        
                                extracted_cache[pdf_num] = new_text
                                save_extracted_cache(safe_title, extracted_cache)
                                
                                # Save human-readable offline copy
                                book_offline_dir = os.path.join(OFFLINE_DIR, safe_title)
                                if not os.path.exists(book_offline_dir):
                                    os.makedirs(book_offline_dir)
                                    
                                with open(os.path.join(book_offline_dir, f"Page_{pdf_num}.txt"), "w", encoding="utf-8") as text_file:
                                    text_file.write(new_text)
                            else:
                                extracted_cache[pdf_num] = ""
                                
                        full_ai_text = extracted_cache[pdf_num]
                        
                        if not full_ai_text:
                            continue
                            
                        old_snippet = page_data["old_texts"].get(str(pdf_num), "")
                        if old_snippet and len(old_snippet) < len(full_ai_text) * 0.8:
                            log_area.text(f"   ➔ Slicing chunk for page {pdf_num}...")
                            chunk_to_inject = fuzzy_slice(full_ai_text, old_snippet)
                        else:
                            chunk_to_inject = full_ai_text

                        if is_inherited:
                            if old_snippet and old_snippet in current_wikitext:
                                current_wikitext = current_wikitext.replace(old_snippet, chunk_to_inject)
                        else:
                            current_wikitext, err = inject_text_into_page(current_wikitext, label, chunk_to_inject, master_pdf_filename)
                            
                    final_wikitext = cleanup_page_seams(current_wikitext)
                    res = upload_to_bahaiworks(subpage_title, final_wikitext, "Bot: Sequential Reproofread", session=session)
                    
                    if res.get('edit', {}).get('result') == 'Success':
                        if "completed_subpages" not in state: state["completed_subpages"] = []
                        state["completed_subpages"].append(subpage_title)
                        save_book_state(safe_title, state)
                    else:
                        log_area.text(f"❌ Upload failed for {subpage_title}: {res}")
                        
                    progress_bar.progress((idx + 1) / len(subpages_to_process))
                    time.sleep(1) 

                queue_data[target_book]["status"] = "COMPLETED"
                queue_data[target_book]["last_updated"] = time.time()
                save_queue(queue_data)
                st.success(f"✅ Finished Book: {target_book}")
                st.session_state.pop('running_book', None)
