import streamlit as st
import os
import sys
import json
import re
import time
import requests
import fitz  # PyMuPDF
import concurrent.futures
import threading
import math
import unicodedata
from urllib.parse import unquote

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.batch_worker import process_pdf_batch, process_single_page, _fitz_lock
from src.mediawiki_uploader import (
    upload_to_bahaiworks, 
    API_URL, 
    fetch_wikitext,
    fetch_wikitext_batch,
    inject_text_into_page, 
    get_csrf_token,
    cleanup_page_seams,
    update_header_ps_tag
)
from src.gemini_processor import apply_chunked_split
from src.processor import analyze_split_boundaries, split_pdf_doubles
from src.calibration import calculate_start_offset
from src.ocr_engine import OcrEngine, OcrConfig
from src.text_processing import parse_text_file, find_best_match_for_title

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
    """Deletes {{ocr}} and {{Publicationinfo}}, removes blank pages, cleans seams, injects or updates the {{header}}, and appends __NOTOC__."""
    text = re.sub(r'\{\{ocr.*?\}\}\n?', '', text, flags=re.IGNORECASE)
    
    # Delete {{Publicationinfo}}
    text = re.sub(r'\{\{Publicationinfo.*?\}\}\n?', '', text, flags=re.IGNORECASE)
    
    # Delete blank pages including the {{page}} reference
    text = re.sub(r'\{\{page\|[^}]*\}\}\s*--BLANK--\s*\n?', '', text, flags=re.IGNORECASE)
    
    # 1. Clean seams AFTER all text chunks (like overflow) have been combined
    text = cleanup_page_seams(text)
    
    # 2. Header Logic
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
        
    # 3. Ensure __NOTOC__ is at the bottom
    if "__NOTOC__" not in text:
        text += "\n__NOTOC__"
        
    return text

st.set_page_config(page_title="Book Re-Proofreader", page_icon="📚", layout="wide")

st.markdown("""
    <style>
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"] {
            overflow-y: auto !important;
        }
    </style>
""", unsafe_allow_html=True)

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

NAMESPACE_MAP = {
    "edition": 3150,
    "author": 4000,
    "audio": 4010,
    "video": 4020,
    "transcript": 4030,
    "bahaiworks": 4,
    "project": 4,
}

def parse_wiki_toc_for_recovery(target_page, session=None):
    """Fetches the existing main book page from Bahai.works and extracts subpage links."""
    wikitext, err = fetch_wikitext(target_page, session=session)
    if err or not wikitext:
        return None, False, err or "Empty page content."

    has_restricted = bool(re.search(r'\{\{\s*restricted\s+use', wikitext, re.IGNORECASE))

    contents_match = re.search(r'==+\s*Contents\s*==+(.*)', wikitext, re.IGNORECASE | re.DOTALL)
    toc_block = contents_match.group(1) if contents_match else wikitext

    toc_items = []
    link_pattern = re.compile(r'\[\[/([^\]|]+)(?:\|([^\]]+))?\]\]')
    for match in link_pattern.finditer(toc_block):
        subpage_path = match.group(1).strip().replace("_", " ")
        display_title = (match.group(2) or subpage_path.split("/")[-1]).strip()

        page_guess = ""
        sub_text, sub_err = fetch_wikitext(f"{target_page}/{match.group(1).strip()}", session=session)
        if not sub_err and sub_text:
            p_match = re.search(r'\{\{page\|\s*([^|}\s]+)', sub_text, re.IGNORECASE)
            if p_match:
                page_guess = p_match.group(1).strip()

        toc_items.append({
            "title": display_title,
            "page_name": match.group(1).strip(),
            "display_title": display_title,
            "prefix": "",
            "level": 1,
            "page_range": page_guess,
            "author": []
        })

    if not toc_items:
        return None, has_restricted, f"No [[/Subpage]] links found on '{target_page}'."

    return toc_items, has_restricted, None

def get_all_subpages(root_title, session):
    pages = []
    ns_id = 0
    prefix_title = root_title

    if ":" in root_title:
        potential_ns, remainder = root_title.split(":", 1)
        if potential_ns.strip().lower() in NAMESPACE_MAP:
            ns_id = NAMESPACE_MAP[potential_ns.strip().lower()]
            prefix_title = remainder.strip()

    params = {
        "action": "query",
        "list": "allpages",
        "apnamespace": str(ns_id),
        "apprefix": f"{prefix_title}/",
        "aplimit": "500",
        "format": "json"
    }
    
    while True:
        try:
            res = session.get(API_URL, params=params).json()
            chunk = res.get('query', {}).get('allpages', [])
            
            # EXCLUDE AC-Message pages
            for p in chunk:
                if not p['title'].endswith("AC-Message"):
                    pages.append(p['title'])
            
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
    
    # Pre-fetch all subpages in batches of 50 per HTTP request
    batched_texts = fetch_wikitext_batch(subpages, session=session)
    
    for title in subpages:
        text = batched_texts.get(title)
        if not text:
            text, err = fetch_wikitext(title, session=session)
            if err or not text:
                continue
            
        # --- Normalize Custom Templates (e.g. {{swpage}}, {{bwpage}}) ---
        text = normalize_page_templates(text, session)
            
        # --- Skip redirects ---
        if re.match(r'^\s*#redirect\s*\[\[', text, flags=re.IGNORECASE):
            continue
            
        # --- Skip pages that are already proofread ---
        header_match = re.search(r'\{\{header\s*\n.*?\n\}\}', text, re.DOTALL | re.IGNORECASE)
        if header_match:
            header_body = header_match.group(0)
            notes_match = re.search(r'\|\s*notes\s*=\s*(.*?)(?=\n\s*\||$)', header_body, re.DOTALL | re.IGNORECASE)
            # If the notes field exists and contains {{ps|1}}, skip mapping this chapter entirely
            if notes_match and re.search(r'\{\{ps\|\s*1\s*\}\}', notes_match.group(1), re.IGNORECASE):
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
        
        # Ignore common leading tags that shouldn't trigger a split
        prefix_pattern = r'^(?:<accesscontrol>.*?</accesscontrol>\s*|\{\{(?:header|Publicationinfo)(?:[^{}]|\{\{[^}]*\}\})*\}\}\s*)*'
        clean_text = re.sub(prefix_pattern, '', stripped_text, flags=re.IGNORECASE | re.DOTALL)
        
        if clean_text and not clean_text.lower().startswith("{{page|"):
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

def normalize_filename(name):
    """Normalizes spaces/underscores, strips accents/punctuation, and lowercases for fuzzy matching."""
    base, ext = os.path.splitext(name)
    nfkd = unicodedata.normalize('NFKD', base)
    ascii_str = "".join([c for c in nfkd if not unicodedata.combining(c)])
    # Strip commas, colons, apostrophes, and other punctuation
    ascii_str = re.sub(r"[,:;.'’‘`!?\-\(\)\[\]]", "", ascii_str)
    clean_base = re.sub(r"[\s_]+", "_", ascii_str.lower()).strip("_")
    return clean_base + ext.lower()

def find_local_pdf(filename, root_folder):
    if not filename or not root_folder or not os.path.exists(root_folder):
        return None
    target_norm = normalize_filename(filename)
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower() or normalize_filename(f) == target_norm:
                return os.path.join(dirpath, f)
    return None

def resolve_pdf_path(state, root_folder):
    """Returns custom_pdf_path from state if valid, otherwise searches root_folder."""
    custom_path = state.get("custom_pdf_path")
    if custom_path and os.path.isfile(custom_path):
        return custom_path
    master_pdf = state.get("master_pdf")
    if master_pdf:
        return find_local_pdf(master_pdf, root_folder)
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

    # 1. Metric Cards
    ui_holders["m_done"].metric("✅ Completed", f"{completed_count} / {total_pages}")
    ui_holders["m_flight"].metric("⚡ Running Now", len(running_pages))
    ui_holders["m_docai"].metric("🤖 DocAI Fallbacks", len(docai_pages))
    ui_holders["m_err"].metric("❌ Errors", len(error_pages))

    # 2. Only show straggler list when <= 15 pages remain at the very end
    if 0 < len(unfinished_pages) <= 15:
        straggler_str = ", ".join(str(p) for p in unfinished_pages)
        ui_holders["stragglers"].info(f"⏳ **Waiting on final {len(unfinished_pages)} page(s):** `{straggler_str}`")
    else:
        ui_holders["stragglers"].empty()

    # 3. Full-View Color-Coded Page Heatmap (No max-height or scrollbar)
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

    # 4. Filtered Warnings & Fallbacks Log
    if warning_logs:
        ui_holders["warnings"].code("\n".join(warning_logs[-100:]), language="text")
    else:
        ui_holders["warnings"].caption("🎉 No warnings, blocks, or DocAI fallbacks triggered so far.")

    return completed_count == total_pages

def upload_subpages_parallel(subpages_to_upload, pdf_dir, safe_title, state, session, progress_bar, status_text, log_container, max_workers=2):
    """Uploads chapters with low concurrency using a cached CSRF token to avoid MediaWiki DB deadlocks."""
    state_lock = threading.Lock()
    if "uploaded_subpages" not in state:
        state["uploaded_subpages"] = []

    # Warm up the cached CSRF token once before threads start
    get_csrf_token(session)

    total = len(subpages_to_upload)
    results_log = []
    spam_blocked = False
    hard_failures = []

    def _upload_one(sp):
        nonlocal spam_blocked
        safe_sp = sp.replace("/", "_")
        ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
        if not os.path.exists(ch_file_path):
            return ("MISSING", sp, "Local file missing")

        with open(ch_file_path, "r", encoding="utf-8") as f:
            final_wikitext = f.read()

        for attempt in range(4):
            try:
                res = upload_to_bahaiworks(sp, final_wikitext, "Bot: Parallel Batch Reproofread", session=session)
            except Exception as e:
                res = {"error": str(e)}

            if res.get('edit', {}).get('result') == 'Success':
                with state_lock:
                    if sp not in state["uploaded_subpages"]:
                        state["uploaded_subpages"].append(sp)
                    save_book_state(safe_title, state)
                return ("SUCCESS", sp, None)
            else:
                err_str = str(res)
                if "spam filter" in err_str.lower():
                    if attempt == 0:
                        trigger_match = re.search(r'triggered our spam filter:\s*([^\s\'"}]+)', err_str, re.IGNORECASE)
                        if trigger_match:
                            bad_domain = trigger_match.group(1).strip()
                            url_pattern = re.compile(rf'(?<!<nowiki>)(?:https?://|www\.)?[^\s<>\[\]"\'|]*{re.escape(bad_domain)}[^\s<>\[\]"\'|]*', re.IGNORECASE)
                            final_wikitext = url_pattern.sub(lambda m: f"<nowiki>{m.group(0)}</nowiki>", final_wikitext)
                            with open(ch_file_path, "w", encoding="utf-8") as f:
                                f.write(final_wikitext)
                            results_log.append(f"⚠️ Wrapped spam trigger `{bad_domain}` in `<nowiki>` on `{sp}` and retrying...")
                            continue
                    spam_blocked = True
                    return ("SPAM_BLOCKED", sp, err_str)

                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                return ("FAILED", sp, err_str)

        return ("FAILED", sp, "Unknown upload error")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(_upload_one, sp): sp for sp in subpages_to_upload}

    while True:
        done_count = sum(1 for f in futures if f.done())
        progress_bar.progress(done_count / total if total > 0 else 1.0)
        status_text.markdown(f"**Uploading Concurrently ({max_workers} workers):** `{done_count} / {total}` chapters finished")
        if done_count == total:
            break
        time.sleep(0.3)

    executor.shutdown(wait=True)

    for f, sp in futures.items():
        status, _, detail = f.result()
        if status == "MISSING":
            log_container.error(f"❌ Local file missing for {sp}")
            hard_failures.append((sp, detail))
        elif status == "SPAM_BLOCKED":
            log_container.error(f"❌ Spam filter blocked {sp}: {detail}")
        elif status == "FAILED":
            log_container.error(f"❌ Upload failed for {sp}: {detail}")
            hard_failures.append((sp, detail))

    for msg in results_log:
        log_container.warning(msg)

    return spam_blocked, hard_failures

# ==============================================================================
# 3. UI & MAIN LOGIC
# ==============================================================================

st.title("📚 Book Re-Proofreader (Parallel Batch)")

st.sidebar.header("Configuration")
execution_mode = st.sidebar.radio("Execution Mode", ["1 Book (Test)", "All Books (Production)"])
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")
ocr_strategy = st.sidebar.radio("OCR Strategy", ["Gemini (Default)", "DocAI Only"])

session = requests.Session()
try:
    get_csrf_token(session)
except Exception as e:
    st.error(f"Authentication Failed: {e}")
    st.stop()

# --- Master Queue Display ---
queue_data = load_queue()

def reset_book_cache(book_title, root_folder):
    """Deletes the local state JSON and any generated chapter .txt files for a book."""
    safe_t = book_title.replace("/", "_")
    state_file = os.path.join(CACHE_DIR, f"{safe_t}_state.json")
    state_to_delete = load_book_state(safe_t)
    master_pdf = state_to_delete.get("master_pdf")
    if master_pdf:
        local_pdf_path = find_local_pdf(master_pdf, root_folder)
        if local_pdf_path:
            pdf_dir = os.path.dirname(local_pdf_path)
            for sp in state_to_delete.get("subpages", []):
                safe_sp = sp.replace("/", "_")
                ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                if os.path.exists(ch_file_path):
                    os.remove(ch_file_path)
    if os.path.exists(state_file):
        os.remove(state_file)

tab_main, tab_nomap = st.tabs(["📚 Re-Proofreader Pipeline", "🛠️ Fix 'No Map' Books"])

with tab_nomap:
    st.subheader("🛠️ One-Stop 'No Map' Recovery (Page 02 + Page 03 Consolidated)")
    st.caption("Automatically calibrates offsets, generates fallback OCR `.txt` with live progress, imports the existing Wiki TOC, and splits/uploads `{{page}}` tags.")

    no_map_books = sorted([
        title for title, data in queue_data.items()
        if "NO MAP" in data.get("status", "").upper()
    ])
    all_queue_books = sorted(list(queue_data.keys()))
    dropdown_books = no_map_books if no_map_books else all_queue_books

    nm_col1, nm_col2 = st.columns([2, 3])
    with nm_col1:
        nm_book = st.selectbox(
            f"1. Select Book ({len(no_map_books)} 'ERROR: No Map' in queue)",
            options=dropdown_books,
            key="nm_selected_book"
        )

    # Reset splitter state when switching books
    if st.session_state.get("nm_active_book") != nm_book:
        st.session_state["nm_active_book"] = nm_book
        for k in ["nm_pdf_path", "nm_pdf_input_box", "nm_offset", "nm_is_double", "nm_calibrated", "nm_toc_list", "nm_is_copyright", "nm_page_map", "nm_page_order", "nm_splitter_indices"]:
            st.session_state.pop(k, None)
        if nm_book:
            guessed_pdf = find_local_pdf(f"{nm_book}.pdf", input_folder)
            if guessed_pdf:
                st.session_state["nm_pdf_path"] = guessed_pdf
                st.session_state["nm_pdf_input_box"] = guessed_pdf

    with nm_col2:
        if "nm_pdf_input_box" not in st.session_state:
            st.session_state["nm_pdf_input_box"] = st.session_state.get("nm_pdf_path", "")
        nm_pdf_input = st.text_input(
            "2. Full Local PDF Path (Auto-searched in Root Folder if found)",
            placeholder="/path/to/Book.pdf",
            key="nm_pdf_input_box"
        )
        clean_nm_pdf = nm_pdf_input.strip().strip('"').strip("'")
        if clean_nm_pdf != st.session_state.get("nm_pdf_path", ""):
            st.session_state["nm_pdf_path"] = clean_nm_pdf

    valid_pdf = bool(clean_nm_pdf and os.path.isfile(clean_nm_pdf) and clean_nm_pdf.lower().endswith(".pdf"))
    if clean_nm_pdf and not valid_pdf:
        st.error(f"❌ File not found or not a PDF: `{clean_nm_pdf}`")

    if valid_pdf:
        txt_output_path = clean_nm_pdf[:-4] + ".txt"
        txt_already_exists = os.path.exists(txt_output_path)

        # Calibrate only when explicitly requested
        if st.button("🔍 Auto-Calibrate Offset & Fetch Wiki TOC", width="stretch"):
            with st.spinner("🔍 Auto-detecting page offset, double-page layout, and Wiki TOC..."):
                try:
                    with fitz.open(clean_nm_pdf) as pdf:
                        total_pages = len(pdf)
                    start_off, is_dbl = calculate_start_offset(clean_nm_pdf, total_pages)
                    st.session_state["nm_offset"] = start_off if start_off else 1
                    st.session_state["nm_is_double"] = bool(is_dbl)
                except Exception as e:
                    st.warning(f"Auto-calibration fallback used: {e}")
                    st.session_state["nm_offset"] = 1
                    st.session_state["nm_is_double"] = False

                toc_items, has_restricted, toc_err = parse_wiki_toc_for_recovery(nm_book, session=session)
                if toc_err:
                    st.error(f"⚠️ Wiki TOC fetch warning: {toc_err}")
                    st.session_state["nm_toc_list"] = []
                    st.session_state["nm_is_copyright"] = False
                else:
                    st.session_state["nm_toc_list"] = toc_items
                    st.session_state["nm_is_copyright"] = has_restricted

                st.session_state["nm_calibrated"] = True

        with st.container(border=True):
            st.markdown("#### Step A: Prep & Generate Fallback OCR `.txt`")
            c_cfg1, c_cfg2, c_cfg3, c_cfg4, c_cfg5 = st.columns([1.2, 1.5, 1.2, 1.2, 1.5])
            with c_cfg1:
                nm_offset_val = st.number_input(
                    "Offset (Index of Page 1)",
                    min_value=0,
                    value=int(st.session_state.get("nm_offset", 1)),
                    help="0-based index where printed Page 1 starts (same as Page 02 Prep tab)."
                )
            with c_cfg2:
                nm_ranges_str = st.text_input(
                    "Unnumbered Ranges",
                    value="",
                    placeholder="e.g. 48-52, 102-105",
                    help="Optional 0-based PDF page ranges for unnumbered illustrations."
                )
            with c_cfg3:
                nm_lang = st.selectbox("OCR Language", ["eng", "deu", "fra", "fas", "ara"], index=0)
            with c_cfg4:
                st.write("")
                nm_is_dbl = st.checkbox("2-Up Double Page?", value=st.session_state.get("nm_is_double", False))
                nm_use_text_layer = st.checkbox("Use Embedded Text Layer", value=False, help="Skip Tesseract if PDF has clean digital text.")
            with c_cfg5:
                st.write("")
                nm_is_copy = st.checkbox("Copyright Protected?", value=st.session_state.get("nm_is_copyright", False))
                st.session_state["nm_is_copyright"] = nm_is_copy

            btn_col1, btn_col2 = st.columns([2, 2])
            with btn_col1:
                run_prep_label = "🔄 Re-Run OCR & Load Splitter" if txt_already_exists else "🚀 Run OCR & Load Chapter Splitter"
                run_prep_clicked = st.button(run_prep_label, type="primary", width="stretch")
            with btn_col2:
                skip_ocr_clicked = False
                if txt_already_exists:
                    skip_ocr_clicked = st.button("⚡ Existing `.txt` Found — Load Splitter Immediately", width="stretch")

            if run_prep_clicked:
                active_pdf_path = clean_nm_pdf
                final_offset = nm_offset_val
                prog_bar = st.progress(0, text="Starting preparation...")
                status_box = st.empty()

                # 1. Optional 2-up split
                if nm_is_dbl:
                    status_box.info("✂️ Splitting 2-up double-page spreads...")
                    s_start, s_end = analyze_split_boundaries(active_pdf_path)
                    split_name = f"split_{os.path.basename(active_pdf_path)}"
                    split_path = os.path.join(os.path.dirname(active_pdf_path), split_name)
                    if split_pdf_doubles(active_pdf_path, split_path, s_start, s_end):
                        active_pdf_path = split_path
                        st.session_state["nm_pdf_path"] = active_pdf_path
                        st.session_state["nm_pdf_input_box"] = active_pdf_path
                        with fitz.open(active_pdf_path) as pdf:
                            recalc_start, _ = calculate_start_offset(active_pdf_path, len(pdf))
                            final_offset = recalc_start if recalc_start else 0
                        st.toast(f"Split complete! Re-calibrated offset: {final_offset}")
                    else:
                        st.error("Failed to split double-page PDF.")
                        st.stop()

                # 2. Parse illustration ranges
                ranges_list = []
                if nm_ranges_str.strip():
                    for r in nm_ranges_str.split(","):
                        if "-" in r:
                            s, e = r.split("-")
                            ranges_list.append((int(s.strip()), int(e.strip())))

                config = OcrConfig(
                    has_cover_image=True,
                    first_numbered_page_index=final_offset,
                    illustration_ranges=ranges_list,
                    language=nm_lang
                )
                ocr_eng = OcrEngine(active_pdf_path)

                # 3. Run OCR or Text Layer with live progress bar
                if nm_use_text_layer:
                    def _tl_cb(done, total):
                        prog_bar.progress(done / total if total else 1.0, text=f"Extracting text layer: Page {done}/{total}")
                    txt_output_path = ocr_eng.extract_text_layer(config, progress_callback=_tl_cb)
                else:
                    status_box.info("🖼️ Rendering PDF pages to 300 DPI images via pdftoppm (parallel chunks)...")
                    prog_bar.progress(0.05, text="Rendering PDF pages to PNGs...")
                    total_imgs = ocr_eng.generate_images()
                    status_box.info(f"🔍 Running parallel Tesseract OCR across {total_imgs} pages...")

                    def _ocr_cb(done, total):
                        pct = 0.1 + 0.85 * (done / total if total else 1.0)
                        prog_bar.progress(pct, text=f"Tesseract OCR: {done}/{total} pages completed")

                    txt_output_path = ocr_eng.run_ocr(config, progress_callback=_ocr_cb)
                    prog_bar.progress(0.98, text="Cleaning up temporary images...")
                    ocr_eng.cleanup(config)

                prog_bar.progress(1.0, text="✅ Fallback .txt generation complete!")
                status_box.success(f"Saved `{os.path.basename(txt_output_path)}`")
                for k in ["nm_page_map", "nm_page_order", "nm_splitter_indices"]:
                    st.session_state.pop(k, None)
                skip_ocr_clicked = True

            if skip_ocr_clicked and os.path.exists(txt_output_path):
                p_map, p_order = parse_text_file(txt_output_path)
                st.session_state["nm_page_map"] = p_map
                st.session_state["nm_page_order"] = p_order
                st.session_state.pop("nm_splitter_indices", None)
                st.rerun()

        # --- STEP B: INTERACTIVE CHAPTER SPLITTER ---
        if "nm_page_map" in st.session_state and st.session_state.get("nm_toc_list"):
            st.divider()
            st.markdown(f"#### Step B: Verify Chapter Start Pages & Upload (`{len(st.session_state['nm_toc_list'])}` chapters)")

            page_map = st.session_state["nm_page_map"]
            page_order = st.session_state["nm_page_order"]
            toc_list = [
                item for item in st.session_state["nm_toc_list"]
                if item.get("page_name") and str(item.get("page_name")).strip() != ""
            ]

            if not page_order:
                st.error("No `{{page|...}}` tags found in the `.txt` file.")
            else:
                if "nm_splitter_indices" not in st.session_state:
                    indices = {}
                    last_valid_idx = 0
                    for i, item in enumerate(toc_list):
                        title = item["title"]
                        raw_range = str(item.get("page_range", "")).strip()
                        guess = raw_range.split("-")[0].strip() if "-" in raw_range else (raw_range or None)

                        if title.lower() in ["preface", "contents", "introduction", "foreword"] and not guess:
                            guess = find_best_match_for_title(title, page_map, page_order, search_all=False)
                        elif not guess:
                            guess = find_best_match_for_title(title, page_map, page_order, search_all=True, min_index=last_valid_idx)

                        if guess and guess in page_order:
                            idx = page_order.index(guess)
                            if idx < last_valid_idx and not raw_range:
                                idx = last_valid_idx
                        else:
                            idx = last_valid_idx

                        indices[i] = idx
                        last_valid_idx = idx
                    st.session_state["nm_splitter_indices"] = indices

                def _nm_adjust_idx(i, direction):
                    curr = st.session_state["nm_splitter_indices"][i]
                    new = curr + direction
                    if 0 <= new < len(page_order):
                        st.session_state["nm_splitter_indices"][i] = new
                        st.session_state[f"nm_lbl_{i}"] = page_order[new]

                def _nm_set_lbl(i):
                    typed = st.session_state.get(f"nm_lbl_{i}", "").strip()
                    if typed in page_order:
                        st.session_state["nm_splitter_indices"][i] = page_order.index(typed)

                for i, item in enumerate(toc_list):
                    curr_idx = st.session_state["nm_splitter_indices"][i]
                    curr_lbl = page_order[curr_idx]
                    if f"nm_lbl_{i}" not in st.session_state:
                        st.session_state[f"nm_lbl_{i}"] = curr_lbl

                    c_title, c_nav, c_prev = st.columns([2, 1, 4])
                    with c_title:
                        st.subheader(item.get("page_name", item["title"]))
                        st.caption(f"Hint: {item.get('page_range') or 'Auto-matched'}")
                    with c_nav:
                        st.text_input(
                            "Page Tag",
                            key=f"nm_lbl_{i}",
                            on_change=_nm_set_lbl,
                            args=(i,)
                        )
                        b_m, b_p = st.columns(2)
                        with b_m:
                            st.button("◀", key=f"nm_prev_{i}", width="stretch", on_click=_nm_adjust_idx, args=(i, -1))
                        with b_p:
                            st.button("▶", key=f"nm_next_{i}", width="stretch", on_click=_nm_adjust_idx, args=(i, 1))
                    with c_prev:
                        p_text = page_map.get(curr_lbl, "Error: Content missing")
                        st.text_area("Preview", value=p_text[:400] + "...", height=120, key=f"nm_pv_{i}_{curr_idx}", disabled=True)
                    st.divider()

                if st.button("✂️ Split, Upload to Bahai.works & Reset to PENDING", type="primary", width="stretch"):
                    target_base = nm_book
                    pdf_filename = os.path.basename(st.session_state["nm_pdf_path"])
                    is_copyright = st.session_state.get("nm_is_copyright", False)

                    if is_copyright:
                        access_group = target_base.replace(" ", "")
                        header_content = f"<accesscontrol>Access:{access_group}</accesscontrol>{{{{Publicationinfo}}}}\n"
                    else:
                        header_content = "{{Publicationinfo}}\n"

                    up_prog = st.progress(0)
                    up_status = st.empty()

                    try:
                        if is_copyright:
                            file_page_title = f"File:{pdf_filename}"
                            up_status.write(f"🔒 Securing {file_page_title}...")
                            upload_to_bahaiworks(
                                file_page_title,
                                f"<accesscontrol>Access:{access_group}</accesscontrol>",
                                "Automated: Applied Access Control tag to File page",
                                check_exists=False,
                                session=session
                            )

                        final_split_data = [
                            {
                                "title": item["title"],
                                "page_name": item.get("page_name", item["title"]),
                                "start_idx": st.session_state["nm_splitter_indices"][i],
                                "original_item": item
                            }
                            for i, item in enumerate(toc_list)
                        ]

                        for i, chapter in enumerate(final_split_data):
                            start_idx = chapter["start_idx"]
                            end_idx = max(start_idx, final_split_data[i + 1]["start_idx"]) if i + 1 < len(final_split_data) else len(page_order)
                            safe_end_idx = max(start_idx, end_idx - 1)
                            chapter["original_item"]["page_range"] = f"{page_order[start_idx]}-{page_order[safe_end_idx]}"

                            up_status.write(f"Uploading `{target_base}/{chapter['page_name']}` ({i+1}/{len(final_split_data)})...")
                            raw_text = "".join(
                                page_map.get(page_order[p_idx], "") + "\n\n"
                                for p_idx in range(start_idx, end_idx)
                                if 0 <= p_idx < len(page_order)
                            )
                            full_content = f"{header_content}\n{raw_text}"
                            upload_to_bahaiworks(f"{target_base}/{chapter['page_name']}", full_content, "Splitter Upload", session=session)
                            up_prog.progress((i + 1) / len(final_split_data))

                        # Save toc.json alongside PDF
                        toc_path = os.path.join(os.path.dirname(st.session_state["nm_pdf_path"]), "toc.json")
                        with open(toc_path, "w", encoding="utf-8") as f:
                            json.dump(st.session_state["nm_toc_list"], f, indent=2, ensure_ascii=False)

                        # Clear cached state, save custom_pdf_path, and mark book PENDING in queue
                        reset_book_cache(target_base, input_folder)
                        new_state = load_book_state(target_base.replace("/", "_"))
                        new_state["custom_pdf_path"] = st.session_state["nm_pdf_path"]
                        save_book_state(target_base.replace("/", "_"), new_state)

                        queue_data[target_base] = {"status": "PENDING", "last_updated": time.time()}
                        save_queue(queue_data)
                        st.session_state["selected_book"] = target_base

                        for k in ["nm_page_map", "nm_page_order", "nm_splitter_indices", "nm_calibrated"]:
                            st.session_state.pop(k, None)

                        up_status.success(f"🎉 Uploaded all chapters for **{target_base}** and reset queue status to `PENDING`! Switch to Tab 1 ('📚 Re-Proofreader Pipeline') to run it.")
                        time.sleep(1.5)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Upload failed: {e}")

with tab_main:
    col_input, col_add_btn = st.columns([4, 1])
    with col_input:
        new_book_input = st.text_input(
            "Load Book (Paste Bahai.works URL or Wiki Title)", 
            placeholder="e.g., https://bahai.works/The_B%C3%A1b_(Balyuzi) or The Báb (Balyuzi)",
            label_visibility="collapsed"
        )
    with col_add_btn:
        if st.button("🎯 Load Book", width='stretch'):
            if new_book_input.strip():
                raw_val = new_book_input.strip()
                if "bahai.works/" in raw_val:
                    raw_val = raw_val.split("bahai.works/")[-1].split("?")[0].split("#")[0]
                clean_title = unquote(raw_val).replace("_", " ").strip("/")
                
                if clean_title not in queue_data:
                    queue_data[clean_title] = {"status": "PENDING"}
                    save_queue(queue_data)
                    
                st.session_state["selected_book"] = clean_title
                st.rerun()

    if queue_data:
        with st.expander("📋 Processing Queue & Triage", expanded=True):
            # Categorize statuses so all "ERROR: ..." variations group cleanly
            def get_status_category(status_str):
                s = status_str.upper()
                if s.startswith("ERROR"): return "ERROR"
                if s in ["PENDING", "READY", "SKIPPED", "COMPLETED"]: return s
                return "OTHER"

            counts = {"ERROR": 0, "PENDING": 0, "READY": 0, "SKIPPED": 0, "COMPLETED": 0, "OTHER": 0}
            for data in queue_data.values():
                cat = get_status_category(data.get("status", "UNKNOWN"))
                counts[cat] = counts.get(cat, 0) + 1

            # Display live metrics
            m_cols = st.columns(5)
            m_cols[0].metric("🚨 Errors", counts["ERROR"])
            m_cols[1].metric("⏳ Pending", counts["PENDING"])
            m_cols[2].metric("👀 Ready (Review)", counts["READY"])
            m_cols[3].metric("⏭️ Skipped", counts["SKIPPED"])
            m_cols[4].metric("✅ Completed", counts["COMPLETED"])

            # Filter & Bulk Select Controls
            col_filter, col_select_all = st.columns([4, 1])
            with col_filter:
                all_categories = ["ERROR", "PENDING", "READY", "SKIPPED", "COMPLETED"]
                if counts["OTHER"] > 0:
                    all_categories.append("OTHER")
                default_filters = [c for c in ["ERROR", "PENDING", "READY", "SKIPPED"] if counts[c] > 0]
                if not default_filters:
                    default_filters = all_categories

                selected_categories = st.multiselect(
                    "Filter by Status Category",
                    options=all_categories,
                    default=default_filters
                )
            with col_select_all:
                st.write("")
                st.write("")
                select_all_visible = st.checkbox("Select All Visible", value=False)

            # Build filtered dataframe
            df = []
            for title, data in queue_data.items():
                raw_status = data.get("status", "UNKNOWN")
                if get_status_category(raw_status) in selected_categories:
                    df.append({
                        "Select": select_all_visible,
                        "Book Title": title,
                        "Status": raw_status
                    })

            edited_df = st.data_editor(
                df,
                width='stretch',
                height=350,
                hide_index=True,
                disabled=["Book Title", "Status"],
                column_config={
                    "Select": st.column_config.CheckboxColumn("Select", default=False),
                    "Book Title": st.column_config.TextColumn("Book Title"),
                    "Status": st.column_config.TextColumn("Status"),
                },
                key="queue_table"
            )

            # Multi-Action Toolbar
            b_col1, b_col2, b_col3 = st.columns(3)
            with b_col1:
                if st.button("🔄 Mark Fixed & Reset to PENDING", type="primary", width='stretch', help="Sets status to PENDING and wipes cached state/.txt files so the pipeline can cleanly re-run."):
                    changed = False
                    for row in edited_df:
                        if row.get("Select") and row.get("Book Title") in queue_data:
                            title = row["Book Title"]
                            reset_book_cache(title, input_folder)
                            queue_data[title]["status"] = "PENDING"
                            changed = True
                    if changed:
                        save_queue(queue_data)
                        st.rerun()

            with b_col2:
                if st.button("🚫 Mark SKIPPED", width='stretch'):
                    changed = False
                    for row in edited_df:
                        if row.get("Select") and row.get("Book Title") in queue_data:
                            queue_data[row["Book Title"]]["status"] = "SKIPPED"
                            changed = True
                    if changed:
                        save_queue(queue_data)
                        st.rerun()

            with b_col3:
                if st.button("✅ Force COMPLETED", width='stretch'):
                    changed = False
                    for row in edited_df:
                        if row.get("Select") and row.get("Book Title") in queue_data:
                            queue_data[row["Book Title"]]["status"] = "COMPLETED"
                            changed = True
                    if changed:
                        save_queue(queue_data)
                        st.rerun()

    all_books = list(queue_data.keys())
    if not all_books:
        st.warning("Queue is empty. Check your queue JSON file.")
        st.stop()

    # ==============================================================================
    # AUTOMATED PRODUCTION MODE (ALL BOOKS)
    # ==============================================================================
    if execution_mode == "All Books (Production)":
        st.divider()
        st.header("🤖 Automated Production Mode")
        
        pending_books = [b for b, d in queue_data.items() if d.get("status", "").upper() == "PENDING"]
        
        if not pending_books:
            st.session_state["auto_running"] = False
            st.success("No 'PENDING' books found in the queue.")
            st.stop()
            
        # Sort alphabetically and allow selection of starting point
        pending_books.sort()
        starting_book = st.selectbox("Start Production From:", pending_books)
        start_idx = pending_books.index(starting_book)
        target_pending_books = pending_books[start_idx:]
            
        st.info(f"Found {len(target_pending_books)} book(s) to process starting from `{starting_book}`. The script will process Steps 1-3, auto-upload if no splits are needed, and sleep for 5 minutes between books.")
        
        col_start, col_stop = st.columns([1, 1])
        with col_start:
            if st.button("▶️ Start Automation Loop", type="primary", width='stretch'):
                st.session_state["auto_running"] = True
        with col_stop:
            if st.button("🛑 Stop Execution", width='stretch'):
                st.session_state["auto_running"] = False
                st.warning("Automation stopped.")
                st.stop()
                
        if st.session_state.get("auto_running", False):
            overall_progress = st.progress(0)
            overall_status = st.empty()
            
            for idx, target_book in enumerate(target_pending_books):
                overall_status.markdown(f"### Processing Book {idx+1}/{len(target_pending_books)}: `{target_book}`")
                log_container = st.container(border=True)
                safe_title = target_book.replace("/", "_")
                state = load_book_state(safe_title)
                
                # --- AUTO STEP 1: Generate Map ---
                if not state.get("subpages") or not state.get("route_map") or not state.get("master_pdf"):
                    log_container.write(f"🔍 Generating Chapter Map for {target_book}...")
                    subpages = get_all_subpages(target_book, session)
                    route_map, master_pdf_filename, wikitext_cache, ordered_subpages = build_sequential_route_map(subpages, session, input_folder)
                    
                    if not route_map or not master_pdf_filename:
                        log_container.error(f"❌ Could not find PDF references for {target_book}. Marking ERROR and skipping.")
                        queue_data[target_book]["status"] = "ERROR: No Map"
                        save_queue(queue_data)
                        continue
                        
                    state["subpages"] = ordered_subpages
                    state["route_map"] = route_map
                    state["master_pdf"] = master_pdf_filename
                    state["wikitext_cache"] = wikitext_cache
                    save_book_state(safe_title, state)
                else:
                    log_container.write(f"🗺️ Loaded existing Chapter Map for {target_book} from cache.")
                    ordered_subpages = state["subpages"]
                    route_map = state["route_map"]
                    master_pdf_filename = state["master_pdf"]
                    wikitext_cache = state.get("wikitext_cache", {})
                
                # --- AUTO STEP 2: Generate Master JSON ---
                local_pdf_path = resolve_pdf_path(state, input_folder)
                if not local_pdf_path:
                    log_container.error(f"❌ Local PDF not found for '{master_pdf_filename}'. Marking ERROR and skipping.")
                    queue_data[target_book]["status"] = "ERROR: Missing PDF"
                    save_queue(queue_data)
                    continue
                    
                pdf_dir = os.path.dirname(local_pdf_path)
                master_json_path = os.path.join(pdf_dir, f"master_{master_pdf_filename}.json")
                
                if not os.path.exists(master_json_path):
                    log_container.write(f"⚙️ Generating Master JSON for {master_pdf_filename}...")
                    all_mapped_pages = set()
                    for ch, data in route_map.items():
                        for p in data.get("pdf_pages", []):
                            all_mapped_pages.add(p["pdf_num"])
                    pages_to_process = sorted(list(all_mapped_pages))
                    
                    temp_master_path = os.path.join(pdf_dir, f"temp_master_{master_pdf_filename}.json")
                    shared_results = {}
                    if os.path.exists(temp_master_path):
                        try:
                            with open(temp_master_path, "r", encoding="utf-8") as f:
                                loaded_temp = json.load(f)
                                shared_results = {str(k): v for k, v in loaded_temp.items() if v}
                            if shared_results:
                                log_container.info(f"🔄 Resuming from checkpoint: **{len(shared_results)} / {len(pages_to_process)}** pages already saved.")
                        except json.JSONDecodeError:
                            shared_results = {}

                    ocr_progress_bar = log_container.progress(0)
                    m_cols = log_container.columns(4)
                    straggler_holder = log_container.empty()
                    grid_holder = log_container.empty()
                    with log_container.expander("⚠️ Warnings & DocAI Fallbacks Log", expanded=True):
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
                        
                        pdf_doc = fitz.open(local_pdf_path)
                        executor = concurrent.futures.ThreadPoolExecutor(max_workers=100)
                        page_futures = {
                            page_num: executor.submit(
                                process_single_page, page_num, pdf_doc, ocr_strategy,
                                temp_master_path, shared_results, shared_log_list
                            )
                            for page_num in pages_to_process
                        }
                        
                        # Live visibility loop with 429 monitoring
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
                                log_container.warning(f"⚠️ Fatal API/Network error detected: `{fatal_error_msg}`. Sleeping for 15 minutes before retrying once...")
                                retry_countdown = log_container.empty()
                                for remaining in range(900, 0, -1):
                                    mins, secs = divmod(remaining, 60)
                                    retry_countdown.code(f"Retrying OCR in: {mins:02d}:{secs:02d}")
                                    time.sleep(1)
                                retry_countdown.empty()
                                log_container.info("🔄 Retrying OCR page processing...")
                            else:
                                log_container.error(f"❌ FATAL (Failed twice): `{fatal_error_msg}`. Halting execution.")
                                queue_data[target_book]["status"] = f"ERROR: {fatal_error_msg[:80]}"
                                save_queue(queue_data)
                                st.stop()
                        else:
                            break
                        
                    log_container.write("🔄 Assembling pages into Master JSON...")
                    master_data = {
                        str(k): shared_results[str(k)]
                        for k in sorted((int(x) for x in shared_results.keys()))
                    }
                    with open(master_json_path, 'w', encoding='utf-8') as f:
                        json.dump(master_data, f, indent=4)
                    if os.path.exists(temp_master_path):
                        os.remove(temp_master_path)
                else:
                    log_container.write(f"📄 Master JSON already exists for {master_pdf_filename}.")
                    with open(master_json_path, 'r', encoding='utf-8') as f:
                        master_data = json.load(f)

                # --- CHECK IF SPLIT / MANUAL REVIEW IS NEEDED BEFORE STEP 3 ---
                split_reasons = []
                for sp in state["subpages"]:
                    route_info = state.get("route_map", {}).get(sp, {})
                    if not route_info.get("pdf_pages"):
                        split_reasons.append(f"`{sp}` (Unmapped / No {{page}} tags)")
                    elif route_info.get("needs_split", False):
                        split_reasons.append(f"`{sp}` (Needs Split / Text before first {{page}} tag)")

                if split_reasons:
                    queue_data[target_book]["status"] = "READY"
                    save_queue(queue_data)
                    reasons_str = "\n  - ".join(split_reasons)
                    log_container.warning(
                        f"⏸️ **{target_book}** paused before Step 3 (Marked `READY` for manual split in Test Mode).\n"
                        f"**Chapters requiring split/review:**\n  - {reasons_str}"
                    )
                else:
                    # --- AUTO STEP 3: Offline Batch Processing (No Splits Needed) ---
                    log_container.write("💾 Processing subpages locally...")
                    subpages_to_process = [sp for sp in state["subpages"] if sp not in state.get("completed_subpages", [])]
                    
                    for active_chapter in subpages_to_process:
                        current_wikitext_for_year = state["wikitext_cache"].get(active_chapter, "")
                        found_year = None
                        cat_match = re.search(r'\[\[Category:\s*(\d{4})\s*\]\]', current_wikitext_for_year, re.IGNORECASE)
                        if cat_match: found_year = cat_match.group(1)
                        
                        page_data = state["route_map"].get(active_chapter, {})
                        pdf_targets = page_data.get("pdf_pages", [])
                        pages_to_process_ch = [t["pdf_num"] for t in pdf_targets]
                        all_extracted_text = {int(p): master_data[str(p)] for p in pages_to_process_ch if str(p) in master_data}
                        
                        # Inject current chapter text
                        current_wikitext = state["wikitext_cache"].get(active_chapter, "")
                        for target in pdf_targets:
                            if all_extracted_text.get(target["pdf_num"]):
                                current_wikitext, _ = inject_text_into_page(current_wikitext, target["label"], all_extracted_text[target["pdf_num"]], master_pdf_filename)
                                
                        final_wikitext = current_wikitext
                        preserved_prefix = ""
                        match = re.search(r'\{\{page\|', final_wikitext, flags=re.IGNORECASE)
                        if match:
                            leading_text = final_wikitext[:match.start()]
                            safe_tags = []
                            access_match = re.search(r'<accesscontrol>.*?</accesscontrol>', leading_text, flags=re.IGNORECASE | re.DOTALL)
                            if access_match: safe_tags.append(access_match.group(0))
                            header_match = re.search(r'\{\{header(?:[^{}]|\{\{[^}]*\}\})*\}\}', leading_text, flags=re.IGNORECASE | re.DOTALL)
                            if header_match: safe_tags.append(header_match.group(0))
                            preserved_prefix = "\n".join(safe_tags) + "\n" if safe_tags else ""
                            final_wikitext = final_wikitext[match.start():]
                        else:
                            final_wikitext = ""
                            
                        final_wikitext = preserved_prefix + final_wikitext
                        final_wikitext = apply_final_formatting(final_wikitext, active_chapter, found_year)
                        
                        safe_sp = active_chapter.replace("/", "_")
                        with open(os.path.join(pdf_dir, f"{safe_sp}.txt"), "w", encoding="utf-8") as f:
                            f.write(final_wikitext)
                            
                        state["completed_subpages"].append(active_chapter)
                        save_book_state(safe_title, state)
                        
                    # --- AUTO STEP 4: Wiki Upload ---
                    log_container.write("🌐 No split logic required. Auto-uploading to Wiki in parallel...")
                    remaining_uploads = [sp for sp in state["subpages"] if sp not in state.get("uploaded_subpages", [])]
                    up_prog = log_container.progress(0)
                    up_status = log_container.empty()

                    spam_blocked_book, hard_failures = upload_subpages_parallel(
                        remaining_uploads, pdf_dir, safe_title, state, session, up_prog, up_status, log_container
                    )

                    if hard_failures and not spam_blocked_book:
                        log_container.warning(f"⚠️ {len(hard_failures)} chapter(s) failed upload. Sleeping 15 minutes before retrying remaining chapters...")
                        retry_countdown = log_container.empty()
                        for remaining in range(900, 0, -1):
                            mins, secs = divmod(remaining, 60)
                            retry_countdown.code(f"Retrying upload in: {mins:02d}:{secs:02d}")
                            time.sleep(1)
                        retry_countdown.empty()

                        remaining_uploads = [sp for sp in state["subpages"] if sp not in state.get("uploaded_subpages", [])]
                        spam_blocked_book, hard_failures = upload_subpages_parallel(
                            remaining_uploads, pdf_dir, safe_title, state, session, up_prog, up_status, log_container
                        )
                        if hard_failures and not spam_blocked_book:
                            log_container.error("❌ Upload failed a second time. Halting pipeline.")
                            queue_data[target_book]["status"] = "ERROR: Upload Failed"
                            save_queue(queue_data)
                            st.stop()
                    
                    if spam_blocked_book:
                        save_book_state(safe_title, state)
                        queue_data[target_book]["status"] = "ERROR: Spam Filter"
                        save_queue(queue_data)
                        continue
                    
                    save_book_state(safe_title, state)
                    queue_data[target_book]["status"] = "COMPLETED"
                    save_queue(queue_data)
                    
                    # Cleanup local chapter .txt files
                    deleted_count = 0
                    for sp in state["subpages"]:
                        safe_sp = sp.replace("/", "_")
                        ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                        if os.path.exists(ch_file_path):
                            os.remove(ch_file_path)
                            deleted_count += 1
                            
                    log_container.success(f"✅ {target_book} automatically uploaded, marked COMPLETED, and cleaned up ({deleted_count} local .txt files removed).")
                    
                overall_progress.progress((idx + 1) / len(target_pending_books))
                
                # Sleep logic between books
                if idx < len(target_pending_books) - 1:
                    log_container.info("⏳ Sleeping for 5 minutes before processing the next book...")
                    col_timer, col_skip = log_container.columns([3, 1])
                    countdown = col_timer.empty()
                    if col_skip.button("⏭️ Skip Wait", key=f"skip_wait_{idx}", width='stretch'):
                        st.rerun()
                    for remaining in range(300, 0, -1):
                        mins, secs = divmod(remaining, 60)
                        hours, mins = divmod(mins, 60)
                        countdown.code(f"Next book starts in: {hours:02d}:{mins:02d}:{secs:02d}")
                        time.sleep(1)
                    countdown.empty()
                    
            st.session_state["auto_running"] = False
            st.success("🎉 Automated queue complete! Switch back to '1 Book (Test)' to manually upload books marked as READY.")
            
        st.stop()

    # ==============================================================================
    # MANUAL MODE UI HERE
    # ==============================================================================
    st.divider()

    # --- Select Any Book & Reset State ---
    col1, col2 = st.columns([3, 1])
    with col1:
        if "selected_book" not in st.session_state or st.session_state["selected_book"] not in all_books:
            st.session_state["selected_book"] = all_books[0]
        target_book = st.selectbox("Select Target Book (All Statuses Available)", all_books, key="selected_book")
    with col2:
        st.write("")
        st.write("")
        if st.button("🗑️ Reset Book State", width='stretch', help="Deletes the local map cache so you can re-process a completed book."):
            safe_title = target_book.replace("/", "_")
            state_file = os.path.join(CACHE_DIR, f"{safe_title}_state.json")
            
            # --- Clean up chapter .txt files ---
            state_to_delete = load_book_state(safe_title)
            master_pdf = state_to_delete.get("master_pdf")
            if master_pdf:
                local_pdf_path = resolve_pdf_path(state_to_delete, input_folder)
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
    st.divider()
    st.subheader("Step 1: Generate Chapter Map")

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
    with st.expander(f"📖 Map: {target_book} ({len(state['subpages'])} subpages)", expanded=False):
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
        st.dataframe(map_display, width='stretch', height=250, hide_index=True)

    # ==============================================================================
    # STEP 2: MASTER JSON GENERATION
    # ==============================================================================
    st.divider()
    st.subheader("Step 2: Generate Master JSON")

    local_pdf_path = resolve_pdf_path(state, input_folder)
    pdf_dir = os.path.dirname(local_pdf_path) if local_pdf_path else ""
    master_json_path = os.path.join(pdf_dir, f"master_{state.get('master_pdf')}.json") if pdf_dir else ""
    master_json_exists = os.path.exists(master_json_path) if master_json_path else False

    if not local_pdf_path:
        st.error(
            f"❌ **Local PDF Not Found:** Could not locate `{state.get('master_pdf')}` "
            f"inside `{input_folder}`."
        )
        col_p1, col_p2 = st.columns([4, 1])
        with col_p1:
            manual_pdf_input = st.text_input(
                "Paste Direct Full Path to Local PDF:",
                value=state.get("custom_pdf_path", ""),
                placeholder="/media/sarah/4TB/Projects/.../Book.pdf"
            )
        with col_p2:
            st.write("")
            st.write("")
            if st.button("💾 Save PDF Path", type="primary", width='stretch'):
                clean_path = manual_pdf_input.strip().strip('"').strip("'")
                if os.path.isfile(clean_path):
                    state["custom_pdf_path"] = clean_path
                    save_book_state(safe_title, state)
                    
                    # Also write out initial chapter .txt files if missing
                    new_pdf_dir = os.path.dirname(clean_path)
                    for sp in state.get("subpages", []):
                        safe_sp = sp.replace("/", "_")
                        ch_file_path = os.path.join(new_pdf_dir, f"{safe_sp}.txt")
                        if not os.path.exists(ch_file_path):
                            with open(ch_file_path, "w", encoding="utf-8") as f:
                                f.write(state.get("wikitext_cache", {}).get(sp, ""))
                    st.rerun()
                else:
                    st.error(f"File does not exist at: `{clean_path}`")
        st.stop()
    elif master_json_exists:
        st.caption(f"📁 Using PDF: `{local_pdf_path}`")
        st.success(f"✅ Master JSON already exists for {state['master_pdf']}!")
        with st.expander("Re-generate Master JSON"):
            st.warning("Only do this if you need to re-OCR the entire document. This will overwrite the existing file.")
            col_gen, col_stop = st.columns([1, 1])
            with col_gen:
                regenerate_master = st.button("🔄 Re-generate Master JSON", width='stretch')
            with col_stop:
                if st.button("🛑 Stop Generation", key="stop_regen", width='stretch'):
                    st.warning("Execution stopped.")
                    st.stop()
    else:
        st.info(f"Master JSON not found for {state['master_pdf']}. This step will OCR all mapped pages.")
        col_gen, col_stop = st.columns([1, 1])
        with col_gen:
            regenerate_master = st.button("🚀 Generate Master JSON", type="primary", width='stretch')
        with col_stop:
            if st.button("🛑 Stop Generation", key="stop_gen", width='stretch'):
                st.warning("Execution stopped.")
                st.stop()

    if master_json_path and (not master_json_exists and locals().get('regenerate_master', False) or locals().get('regenerate_master', False)):
        all_mapped_pages = set()
        for ch, data in state["route_map"].items():
            for p in data.get("pdf_pages", []):
                all_mapped_pages.add(p["pdf_num"])
        pages_to_process = sorted(list(all_mapped_pages))

        if not pages_to_process:
            st.warning("No PDF pages found in the route map to process.")
        else:
            master_log = st.container(border=True)
            master_log.write(f"⚙️ Initiating full OCR processing for {len(pages_to_process)} pages...")
            
            temp_master_path = os.path.join(pdf_dir, f"temp_master_{state['master_pdf']}.json")
            shared_results = {}
            if os.path.exists(temp_master_path):
                try:
                    with open(temp_master_path, "r", encoding="utf-8") as f:
                        loaded_temp = json.load(f)
                        shared_results = {str(k): v for k, v in loaded_temp.items() if v}
                    if shared_results:
                        master_log.info(f"🔄 Resuming from checkpoint: **{len(shared_results)} / {len(pages_to_process)}** pages already saved.")
                except json.JSONDecodeError:
                    shared_results = {}

            ocr_progress_bar = master_log.progress(0)
            m_cols = master_log.columns(4)
            straggler_holder = master_log.empty()
            grid_holder = master_log.empty()
            with master_log.expander("⚠️ Warnings & DocAI Fallbacks Log", expanded=True):
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

            shared_log_list = []

            pdf_doc = fitz.open(local_pdf_path)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=100)
            page_futures = {
                page_num: executor.submit(
                    process_single_page,
                    page_num, pdf_doc, ocr_strategy, 
                    temp_master_path, shared_results, shared_log_list
                )
                for page_num in pages_to_process
            }

            fatal_error = False
            fatal_error_msg = ""
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
                master_log.error(f"❌ FATAL: `{fatal_error_msg}`. Halting execution to prevent raw text saving.")
                st.stop()

            master_log.write("🔄 Assembling pages into Master JSON...")
            master_data = {
                str(k): shared_results[str(k)]
                for k in sorted((int(x) for x in shared_results.keys()))
            }
            
            with open(master_json_path, 'w', encoding='utf-8') as f:
                json.dump(master_data, f, indent=4)
            if os.path.exists(temp_master_path):
                os.remove(temp_master_path)
            
            st.success("🎉 Master JSON successfully generated!")
            st.rerun()

    # Halt execution if Master JSON is not complete
    if not master_json_exists:
        st.stop()


    # --- STEP 3 & 4: AUTOMATED BATCH PROCESSING & WIKI UPLOAD ---
    subpages_to_process = [sp for sp in state["subpages"] if sp not in state.get("completed_subpages", [])]
    subpages_to_upload = [sp for sp in state["subpages"] if sp not in state.get("uploaded_subpages", [])]


    # ==============================================================================
    # STEP 3: OFFLINE BATCH PROCESSING
    # ==============================================================================
    if subpages_to_process:
        st.divider()
        st.subheader("Step 3: Offline Batch Processing")
        st.info(f"Ready to process {len(subpages_to_process)} remaining sections locally.")

        # --- Evaluate if LLM Split is needed ---
        split_reasons = []
        for sp in subpages_to_process:
            route_info = state.get("route_map", {}).get(sp, {})
            if not route_info.get("pdf_pages"):
                split_reasons.append(f"- `{sp}` *(Unmapped / No {{page}} tags)*")
            elif route_info.get("needs_split", False):
                split_reasons.append(f"- `{sp}` *(Needs Split / Text before first {{page}} tag)*")

        split_prompt = ""
        if split_reasons:
            reasons_md = "\n".join(split_reasons)
            st.warning(
                f"⚠️ **The map indicates that the following chapter(s) require splits:**\n{reasons_md}\n\n"
                f"Review the LLM split instruction below before starting."
            )
            split_prompt = st.text_area(
                "Custom Split Instruction", 
                value="Look for the start of a new chapter. The chapter is usually in all caps or a distinct heading.",
                help="Context to help the LLM identify where a specific chapter begins."
            )

        col_start, col_stop = st.columns([1, 1])
        with col_start:
            start_batch = st.button("🚀 Start Processing All Chapters", type="primary", width='stretch')
        with col_stop:
            if st.button("🛑 Stop Execution", width='stretch'):
                st.warning("Execution stopped.")
                st.stop()

        if start_batch:
            progress_bar = st.progress(0)
            status_text = st.empty()
            log_container = st.container(border=True)
            
            # Initialize safe overflow cache
            if "overflow_cache" not in state:
                state["overflow_cache"] = {}
            
            local_pdf_path = resolve_pdf_path(state, input_folder)
            if not local_pdf_path:
                st.error(f"Local PDF not found for '{state['master_pdf']}'")
                st.stop()
                
            pdf_dir = os.path.dirname(local_pdf_path)

            # --- MASTER JSON CHECK & LOAD (MOVED OUTSIDE LOOP) ---
            master_json_path = os.path.join(pdf_dir, f"master_{state['master_pdf']}.json")
            master_data = {}
            
            if os.path.exists(master_json_path):
                log_container.write(f"📄 Master JSON found. Loading text into memory...")
                with open(master_json_path, 'r', encoding='utf-8') as f:
                    master_data = json.load(f)
            else:
                log_container.error(f"❌ Master JSON not found! Please complete Step 2 (Generate Master JSON) first.")
                st.stop()

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
                    
                    # Preserve accesscontrol and header tags before overwriting with overflow
                    safe_tags = []
                    access_match = re.search(r'<accesscontrol>.*?</accesscontrol>', current_wikitext, flags=re.IGNORECASE | re.DOTALL)
                    if access_match:
                        safe_tags.append(access_match.group(0))
                    header_match = re.search(r'\{\{header(?:[^{}]|\{\{[^}]*\}\})*\}\}', current_wikitext, flags=re.IGNORECASE | re.DOTALL)
                    if header_match:
                        safe_tags.append(header_match.group(0))
                        
                    preserved_prefix = "\n".join(safe_tags) + "\n" if safe_tags else ""
                    
                    # If overflow exists from the previous split, it replaces the legacy text entirely
                    if overflow:
                        combined_text = preserved_prefix + overflow.strip()
                    else:
                        combined_text = current_wikitext.strip()
                    
                    if combined_text:
                        log_container.write("💾 Saving unmapped middle section locally...")
                        
                        # --- Apply cleanup, header formatting, and NOTOC ---
                        combined_text = apply_final_formatting(combined_text, active_chapter, found_year)
                        
                        safe_sp = active_chapter.replace("/", "_")
                        ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                        with open(ch_file_path, "w", encoding="utf-8") as f:
                            f.write(combined_text)
                        
                        state["completed_subpages"].append(active_chapter)
                        save_book_state(safe_title, state)
                        continue
                    else:
                        log_container.write("No PDF pages mapped and no cached text. Skipping section.")
                        state["completed_subpages"].append(active_chapter)
                        save_book_state(safe_title, state)
                        continue

                pages_to_process = [t["pdf_num"] for t in pdf_targets]
                
                # --- TEXT EXTRACTION FROM PRE-LOADED JSON ---
                all_extracted_text = {}
                for p_num in pages_to_process:
                    if str(p_num) in master_data:
                        all_extracted_text[int(p_num)] = master_data[str(p_num)]
                    else:
                        log_container.warning(f"⚠️ Page {p_num} missing from master JSON.")

                # --- TARGETED LLM SPLIT LOGIC ---
                # 1. Identify all "ghost chapters" (no mapped pages)
                all_ghost_chapters = [ch for ch in state["subpages"] if not state["route_map"].get(ch, {}).get("pdf_pages")]
                
                # 2. Filter out ghosts that are already completed OR already found and cached
                pending_ghosts = [
                    ch for ch in all_ghost_chapters 
                    if ch not in state.get("completed_subpages", []) 
                    and ch not in state.get("overflow_cache", {}) 
                    and ch != active_chapter
                ]
                
                # 3. Check if the NEXT chapter explicitly needs a split
                next_chapter_needs_split = False
                if next_chapter:
                    next_chapter_needs_split = state["route_map"].get(next_chapter, {}).get("needs_split", False)

                # Trigger LLM if we are still hunting for unfound ghost chapters, OR if the next chapter needs a split
                if pages_to_process and (pending_ghosts or next_chapter_needs_split):
                    last_page_num = pages_to_process[-1]
                    last_page_text = all_extracted_text.get(last_page_num, "")
                    
                    if last_page_text:
                        if next_chapter_needs_split:
                            log_container.write(f"🧠 Asking LLM to split page {last_page_num} (Next chapter requires split)...")
                        else:
                            log_container.write(f"🧠 Asking LLM to scan page {last_page_num} (Because {len(pending_ghosts)} unmapped ghost chapter(s) exist)...")
                        
                        # Pass the next chapter AND the remaining unfound ghost chapters
                        unmapped_to_pass = [ch for ch in pending_ghosts if ch != next_chapter]
                        
                        split_results = apply_chunked_split(last_page_text, next_chapter, unmapped_to_pass, split_prompt)
                        
                        if "_previous_" in split_results:
                            all_extracted_text[last_page_num] = split_results["_previous_"]
                            
                        for found_chap, text_content in split_results.items():
                            if found_chap == "_previous_" or not text_content.strip(): 
                                continue
                            
                            # Store remainder safely in overflow cache
                            existing_overflow = state["overflow_cache"].get(found_chap, "")
                            
                            # Prevent duplicates and append
                            if text_content.strip() not in existing_overflow:
                                state["overflow_cache"][found_chap] = f"{existing_overflow}\n\n{text_content}".strip()

                # Inject Current Chapter
                current_wikitext = state["wikitext_cache"].get(active_chapter, "")
                for target in pdf_targets:
                    pdf_num = target["pdf_num"]
                    label = target["label"]
                    chunk_to_inject = all_extracted_text.get(pdf_num, "")
                    
                    if chunk_to_inject:
                        current_wikitext, err = inject_text_into_page(current_wikitext, label, chunk_to_inject, state["master_pdf"])

                # Final Cleanup & Local Save (NO WIKI UPLOAD)
                final_wikitext = current_wikitext
                preserved_prefix = ""
                
                # Clear legacy unproofread text before the first {{page| template
                if pdf_targets:
                    match = re.search(r'\{\{page\|', final_wikitext, flags=re.IGNORECASE)
                    if match:
                        leading_text = final_wikitext[:match.start()]

                        # Explicitly preserve accesscontrol tags and the header
                        safe_tags = []
                        access_match = re.search(r'<accesscontrol>.*?</accesscontrol>', leading_text, flags=re.IGNORECASE | re.DOTALL)
                        if access_match:
                            safe_tags.append(access_match.group(0))
                            
                        header_match = re.search(r'\{\{header(?:[^{}]|\{\{[^}]*\}\})*\}\}', leading_text, flags=re.IGNORECASE | re.DOTALL)
                        if header_match:
                            safe_tags.append(header_match.group(0))

                        preserved_prefix = "\n".join(safe_tags) + "\n" if safe_tags else ""

                        final_wikitext = final_wikitext[match.start():]
                    else:
                        # If no page template is found, delete the legacy content entirely
                        final_wikitext = ""
                
                # Pull any overflow belonging to this chapter and prepend it
                overflow = state.get("overflow_cache", {}).get(active_chapter, "")
                if overflow:
                    final_wikitext = f"{overflow}\n\n{final_wikitext}".strip()
                    
                # Reattach the preserved prefix at the very top so apply_final_formatting can see it
                final_wikitext = preserved_prefix + final_wikitext
                    
                # --- Apply Header, Seam Cleanup, NOTOC, & OCR Cleanup ---
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
            st.rerun()

        st.stop() # Halts execution here so the wiki upload UI below is hidden until offline is complete


    # ==============================================================================
    # STEP 4: WIKI UPLOAD PHASE (Runs when Offline is complete)
    # ==============================================================================
    st.success(f"✅ Offline processing complete for {target_book}! Local txt files are ready for review.")

    if subpages_to_upload:
        st.divider()
        st.subheader("Step 4: Wiki Upload Phase")
        st.info(f"Ready to upload {len(subpages_to_upload)} sections to the wiki.")

        if st.button("🌐 Upload All Chapters to Wiki", type="primary", width='stretch'):
            progress_bar = st.progress(0)
            status_text = st.empty()
            log_container = st.container(border=True)
            
            local_pdf_path = resolve_pdf_path(state, input_folder)
            pdf_dir = os.path.dirname(local_pdf_path) if local_pdf_path else ""
            
            spam_blocked, hard_failures = upload_subpages_parallel(
                subpages_to_upload, pdf_dir, safe_title, state, session, progress_bar, status_text, log_container
            )

            if spam_blocked or hard_failures:
                st.stop()
                    
            progress_bar.progress(1.0)
            queue_data[target_book]["status"] = "COMPLETED"
            save_queue(queue_data)
            status_text.success("🎉 All chapters successfully uploaded to the wiki!")
            time.sleep(1)
            st.rerun()

    # ==============================================================================
    # STEP 5: CLEANUP PHASE
    # ==============================================================================
    else:
        st.divider()
        st.subheader("Step 5: Cleanup Phase")
        st.success(f"🎉 All sections for {target_book} have been uploaded to the wiki!")
        
        if st.button("🧹 Finalize & Cleanup Local Text Files", type="primary", width='stretch'):
            
            # Ensure the queue status is explicitly marked as COMPLETED
            queue_data[target_book]["status"] = "COMPLETED"
            save_queue(queue_data)
            
            # Locate the directory and delete the .txt files
            local_pdf_path = resolve_pdf_path(state, input_folder)
            if local_pdf_path:
                pdf_dir = os.path.dirname(local_pdf_path)
                deleted_count = 0
                
                for sp in state.get("subpages", []):
                    safe_sp = sp.replace("/", "_")
                    ch_file_path = os.path.join(pdf_dir, f"{safe_sp}.txt")
                    
                    if os.path.exists(ch_file_path):
                        os.remove(ch_file_path)
                        deleted_count += 1
                
                st.success(f"✅ Cleanup complete! {deleted_count} local `.txt` files were removed. The Master JSON remains intact.")
            else:
                st.error("❌ Could not locate the local PDF directory to perform cleanup.")
