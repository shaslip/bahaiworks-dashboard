import streamlit as st
import pandas as pd
import difflib
import re
import os
import sys
import io
import gzip
import xml.etree.ElementTree as ET
import urllib.parse
import requests
import sqlite3
from PIL import Image
import fitz  # PyMuPDF
import google.generativeai as genai
from src.gemini_processor import proofread_page

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# We do NOT import src.database because it points to the local 'documents' DB,
# but we need to query 'knowledge.db' which has a different schema.

from src.mediawiki_uploader import upload_to_bahaiworks, API_URL

# --- Configuration ---
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found. Check your .env file.")
    st.stop()

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

st.set_page_config(page_title="Noisy Page Proofreader", page_icon="🛡️", layout="wide")

# ==============================================================================
# 1. DATABASE HELPERS (Direct Connection to knowledge.db)
# ==============================================================================

def get_default_xml_path():
    """
    Auto-detects the XML dump in the project's 'xml' folder.
    Returns the absolute path to the first .xml.gz file found.
    """
    xml_dir = os.path.join(project_root, 'xml')
    if not os.path.exists(xml_dir):
        return None, f"Directory not found: {xml_dir}"
        
    # Find all .xml.gz files
    candidates = [f for f in os.listdir(xml_dir) if f.endswith('.xml.gz')]
    
    if not candidates:
        return None, f"No .xml.gz files found in {xml_dir}"
        
    # return the full path to the first one found
    return os.path.join(xml_dir, candidates[0]), None

def fetch_from_xml(xml_path, target_page_id):
    """
    Scans the local XML dump for a specific page ID and extracts the text.
    """
    if not os.path.exists(xml_path):
        return None, f"XML Dump not found at: {xml_path}"

    target_page_id = str(target_page_id)
    
    try:
        with gzip.open(xml_path, 'rb') as f:
            # We use iterparse for memory efficiency
            context = ET.iterparse(f, events=('end',))
            
            for event, elem in context:
                if elem.tag.endswith('page'):
                    # Check if this is the page we want
                    id_elem = elem.find('{*}id')
                    if id_elem is not None and id_elem.text == target_page_id:
                        # Found it! Extract text.
                        text_elem = elem.find('.//{*}text')
                        if text_elem is not None:
                            return text_elem.text, None
                        return None, "Page found, but no text content."
                    
                    # Clear element to save memory
                    elem.clear()
                    
        return None, f"Page ID {target_page_id} not found in dump."
        
    except Exception as e:
        return None, f"XML Parse Error: {e}"

def generate_bahai_works_url(title, page_num):
    # Encodes title safely: "Child's Way" -> "Child%27s_Way"
    safe_title = title.replace(" ", "_")
    safe_title = urllib.parse.quote(safe_title)
    return f"https://bahai.works/{safe_title}#pg{page_num}"

def get_knowledge_db_connection():
    """
    Connects specifically to the imported knowledge.db file 
    located in the project root.
    """
    db_path = os.path.join(project_root, 'knowledge.db')
    
    if not os.path.exists(db_path):
        st.error(f"CRITICAL: knowledge.db not found at {db_path}. Please copy it to the project root.")
        st.stop()
        
    return sqlite3.connect(db_path)

def get_noisy_pages_from_db(min_noise=20, limit=50):
    """
    Queries knowledge.db for pages with high noise.
    Uses a Window Function to grab the text content of the *noisiest* segment 
    for the preview snippet.
    """
    conn = get_knowledge_db_connection()
    try:
        query = f"""
            WITH RankedSegments AS (
                SELECT 
                    s.id,
                    s.article_id,
                    s.physical_page_number,
                    s.ocr_noise_score,
                    s.text_content,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.article_id, s.physical_page_number 
                        ORDER BY s.ocr_noise_score DESC
                    ) as rn
                FROM content_segments s
            )
            SELECT 
                a.id as article_db_id, -- Added
                a.title,
                a.source_code,
                a.source_page_id,      -- Added: Needed for XML lookup
                a.language_code,       -- Added: Needed for XML lookup
                rs.physical_page_number,
                rs.ocr_noise_score as max_seg_noise,
                rs.text_content as snippet
            FROM RankedSegments rs
            JOIN articles a ON rs.article_id = a.id
            WHERE rs.rn = 1 AND rs.ocr_noise_score >= ?
            ORDER BY rs.ocr_noise_score DESC
            LIMIT ?
        """
        df = pd.read_sql(query, conn, params=(min_noise, limit))
        return df
    except Exception as e:
        st.error(f"Database Query Error: {e}")
        return pd.DataFrame()
    finally:
        conn.close()

# ==============================================================================
# 2. CORE LOGIC: SMART DIFF & TEXT PROCESSING
# ==============================================================================

def calculate_noise_local(text: str) -> float:
    """Calculates noise score for a specific text chunk."""
    if not text: return 0.0
    allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n\t .,!?'\"()[]-")
    noise_chars = sum(1 for char in text if char not in allowed_chars)
    return (noise_chars / len(text)) * 100

def generate_smart_diff(original: str, new: str) -> str:
    """
    Generates HTML for a 'Smart Diff'.
    - RED BACKGROUND: Mutation (Original was clean, but AI changed it).
    - GREEN BACKGROUND: Restoration (Original was noise, AI fixed it).
    """
    matcher = difflib.SequenceMatcher(None, original, new)
    html = []
    
    for opcode, a0, a1, b0, b1 in matcher.get_opcodes():
        old_chunk = original[a0:a1]
        new_chunk = new[b0:b1]
        
        if opcode == 'equal':
            html.append(f"<span>{new_chunk}</span>")
        
        elif opcode == 'delete':
            # AI removed text.
            noise = calculate_noise_local(old_chunk)
            color = "#ffcccc" if noise < 20 else "#e6ffe6" # Red if clean, Green if noise
            html.append(f"<del style='background-color:{color}; text-decoration: line-through;'>{old_chunk}</del>")
            
        elif opcode == 'insert':
            # AI added text. 
            html.append(f"<ins style='background-color:#e6ffe6;'>{new_chunk}</ins>")
            
        elif opcode == 'replace':
            # The Critical Zone
            noise = calculate_noise_local(old_chunk)
            
            # Logic: If original was >30% noise, it's a FIX (Green).
            # If original was <30% noise, it's a MUTATION (Red).
            if noise > 30:
                style = "background-color:#e6ffe6; color: #006600;" # Green (Safe Fix)
                title = f"Restored (Noise: {noise:.1f}%)"
            else:
                style = "background-color:#ffcccc; color: #cc0000; font-weight:bold; border-bottom: 2px solid red;" # Red (Danger)
                title = f"MUTATION WARNING (Original was clean)"
            
            html.append(f"<span title='{title}' style='{style}'>{new_chunk}</span>")
            
    return "".join(html)

def extract_page_content_by_tag(wikitext: str, physical_page_num: int):
    """
    Parses live wikitext to find content for a specific PHYSICAL page.
    Matches: {{page|19|...}} where 19 is the physical_page_num.
    """
    # Regex: {{page | 19 | ... }}
    # We escape the pipes and allow for spaces
    pattern = re.compile(r'(\{\{page\s*\|\s*' + str(physical_page_num) + r'\s*\|.*?\}\})', re.IGNORECASE)
    match = pattern.search(wikitext)
    
    if not match:
        return None, 0, 0, None
    
    start_tag_end = match.end()
    full_tag = match.group(1)
    
    # Find the NEXT page tag to define the end of this page
    next_tag_pattern = re.compile(r'\{\{page\|')
    next_match = next_tag_pattern.search(wikitext, start_tag_end)
    
    if next_match:
        end_index = next_match.start()
    else:
        end_index = len(wikitext) 
        
    content = wikitext[start_tag_end:end_index]
    return content, start_tag_end, end_index, full_tag

# ==============================================================================
# 3. API & IMAGE UTILS
# ==============================================================================

@st.cache_data(show_spinner="Extracting PDF image...")
def get_page_image(pdf_folder, filename, page_num):
    path = os.path.join(pdf_folder, filename)
    if not os.path.exists(path):
        return None, f"File not found: {path}"
    
    try:
        doc = fitz.open(path)
        # Load page (0-based index)
        page = doc.load_page(page_num - 1)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 2x zoom for better OCR
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        doc.close()
        return img, None
    except Exception as e:
        return None, str(e)

def get_page_id(title):
    """
    Resolves a Wiki Title to a Page ID using the API.
    Required because the XML Dump lookup relies on Page ID.
    """
    params = {
        "action": "query",
        "titles": title,
        "format": "json"
    }
    try:
        response = requests.get(API_URL, params=params, timeout=5)
        data = response.json()
        
        pages = data.get('query', {}).get('pages', {})
        for pid in pages:
            # If page is missing, API returns "-1" as the key
            if pid != "-1":
                return pid
        return None
    except Exception as e:
        st.error(f"API Error resolving title: {e}")
        return None

def fetch_live_wikitext(title):
    """Fetches the absolute latest revision from the API to prevent overwriting previous edits."""
    params = {
        "action": "query", "prop": "revisions", "titles": title,
        "rvprop": "content", "format": "json", "rvslots": "main"
    }
    try:
        response = requests.get(API_URL, params=params, timeout=5)
        data = response.json()
        pages = data['query']['pages']
        for pid in pages:
            if pid != "-1":
                return pages[pid]['revisions'][0]['slots']['main']['*']
    except Exception as e:
        print(f"Error fetching live text: {e}")
    return None
# ==============================================================================
# 4. UI LAYOUT
# ==============================================================================

# --- Sidebar Controls ---
with st.sidebar:
    st.header("📍 Navigation")
    app_mode = st.radio("Mode", ["Noisy Page Queue", "Batch Processor"])
    
    st.divider()

    if app_mode == "Noisy Page Queue":
        st.header("🔍 Discovery Filters")
        min_noise = st.slider("Min Noise Score", 0, 100, 30)

        if st.button("Refresh Queue"):
            st.session_state.queue_df = get_noisy_pages_from_db(min_noise)
            st.session_state.current_selection = None
            st.session_state.gemini_result = None

# --- State Init ---
if 'queue_df' not in st.session_state:
    st.session_state.queue_df = get_noisy_pages_from_db(30)
if 'current_selection' not in st.session_state:
    st.session_state.current_selection = None
if 'gemini_result' not in st.session_state:
    st.session_state.gemini_result = None
if 'last_pdf_path' not in st.session_state:
    st.session_state.last_pdf_path = ""

# Batch Processor State
if 'batch_page_num' not in st.session_state:
    st.session_state.batch_page_num = 1
if 'batch_title' not in st.session_state:
    st.session_state.batch_title = ""
if 'batch_cached_text' not in st.session_state:
    st.session_state.batch_cached_text = None
if 'batch_cached_title' not in st.session_state:
    st.session_state.batch_cached_title = None

# --- Main View ---
st.title("🛡️ Noisy Page Proofreader")

# ==============================================================================
# MODE 1: NOISY PAGE QUEUE
# ==============================================================================
if app_mode == "Noisy Page Queue":
    # TAB 1: THE QUEUE
    if st.session_state.current_selection is None:
        st.markdown(f"### High Noise Pages ({len(st.session_state.queue_df)})")
        
        if st.session_state.queue_df.empty:
            st.success("No pages found above the noise threshold.")
        else:
            for idx, row in st.session_state.queue_df.iterrows():
                with st.container(border=True):
                    c1, c2, c3 = st.columns([6, 2, 1])
                    url = generate_bahai_works_url(row['title'], row['physical_page_number'])
                    
                    with c1:
                        st.markdown(f"### [{row['title']} (Pg {row['physical_page_number']})]({url})")
                        st.caption(f"Source: {row['source_code'].upper()}")

                    with c2:
                        st.metric("Max Noise", f"{row['max_seg_noise']:.1f}")

                    with c3:
                        if st.button("🛠️ Fix", key=f"btn_{idx}", width='stretch'):
                            st.session_state.current_selection = row
                            st.rerun()

                    st.text_area(
                        label="Preview", 
                        value=row['snippet'], 
                        height=100, 
                        disabled=True, 
                        label_visibility="collapsed",
                        key=f"preview_{idx}"
                    )

    # TAB 2: THE WORKBENCH
    else:
        row = st.session_state.current_selection
        url = generate_bahai_works_url(row['title'], row['physical_page_number'])
        
        st.markdown(f"### Editing: [{row['title']} (Page {row['physical_page_number']})]({url})")
        
        if st.button("← Back to Queue"):
            st.session_state.current_selection = None
            st.session_state.gemini_result = None
            st.rerun()
        
        # --- WORKBENCH LOGIC (Queue Mode) ---
        target_pdf_page = None
        wikitext = None

        with st.status("Loading Page Context (Local)...", expanded=True) as status:
            xml_path, path_error = get_default_xml_path()
            if path_error:
                status.update(label="XML Missing", state="error")
                st.error(path_error)
                st.stop()
                
            st.write(f"📂 Scanning `{os.path.basename(xml_path)}` for Page ID {row['source_page_id']}...")
            wikitext, error = fetch_from_xml(xml_path, row['source_page_id'])
            
            if error or not wikitext:
                status.update(label="XML Load Failed", state="error")
                st.error(error)
                st.stop()
                
            original_content, start_idx, end_idx, page_tag = extract_page_content_by_tag(wikitext, row['physical_page_number'])
            
            if not page_tag:
                status.update(label="Tag Search Failed", state="error")
                st.error(f"Could not find `{{{{page|{row['physical_page_number']}|...}}}}` tag.")
                st.stop()
            
            file_match = re.search(r'file=([^|]+)', page_tag)
            filename = file_match.group(1).strip() if file_match else f"{row['title']}.pdf"

            pdf_idx_match = re.search(r'\|page=(\d+)', page_tag)
            
            if pdf_idx_match:
                target_pdf_page = int(pdf_idx_match.group(1))
                st.write(f"🎯 Mapped to PDF Page Index: **{target_pdf_page}**")
            else:
                status.update(label="PDF Index Missing", state="error")
                st.error(f"Could not find 'page=' parameter in tag: {page_tag}")
                st.stop()
                
            status.update(label="Context Loaded Successfully", state="complete", expanded=False)

        st.info(f"Target PDF: **{filename}**")
        pdf_folder = st.text_input("Enter folder path for this PDF:", value=st.session_state.last_pdf_path)
        img = None
        error = None

        if pdf_folder:
            st.session_state.last_pdf_path = pdf_folder
            with st.spinner("Extracting Page Image..."):
                img, error = get_page_image(pdf_folder, filename, target_pdf_page)
            if error: st.error(f"Could not load PDF: {error}")
        else:
            st.warning("☝️ Please enter the folder path above to load the PDF.")

        # --- EDITOR UI (Reused) ---
        if st.session_state.gemini_result is None:
            c_left, c_right = st.columns([1, 1])
            with c_left:
                st.subheader("Source PDF")
                if img: st.image(img, width='stretch')
            with c_right:
                st.subheader("Original Text (Noisy)")
                st.text_area("Current Content", original_content, height=600, disabled=True)
                if img:
                    st.write("---")
                    if st.button("✨ Run Gemini OCR", type="primary", width='stretch'):
                        with st.spinner("Gemini is reading..."):
                            st.session_state.gemini_result = proofread_page(img)
                        st.rerun()
        else:
            c_img, c_edit, c_diff = st.columns([1, 1, 1])
            with c_img:
                st.markdown("##### 1. Source Image")
                if img: st.image(img, width='stretch')
            with c_edit:
                st.markdown("##### 2. Final Text (Editable)")
                final_text = st.text_area("Final Result", value=st.session_state.gemini_result, height=800, label_visibility="collapsed")
                c_save, c_discard = st.columns([1, 1])
                with c_save:
                    if st.button("💾 Save", type="primary", width='stretch'):
                        new_wikitext = wikitext[:start_idx] + "\n" + final_text.strip() + "\n" + wikitext[end_idx:]
                        summary = f"Proofread Pg {row['physical_page_number']} via Dashboard."
                        res = upload_to_bahaiworks(row['title'], new_wikitext, summary)
                        if res.get('edit', {}).get('result') == 'Success':
                            st.success("Saved!")
                            st.session_state.gemini_result = None
                            st.session_state.current_selection = None
                            st.session_state.queue_df = get_noisy_pages_from_db(min_noise)
                            st.rerun()
                        else: st.error(f"Save failed: {res}")
                with c_discard:
                    if st.button("Discard Changes", width='stretch'):
                        st.session_state.gemini_result = None
                        st.rerun()
            with c_diff:
                st.markdown("##### 3. Smart Diff (Guide)")
                diff_html = generate_smart_diff(original_content, st.session_state.gemini_result)
                st.markdown(f"<div style='border:1px solid #ddd; padding:15px; height:800px; overflow-y:scroll; font-family:monospace; background-color:white; color:black; font-size: 0.9em;'>{diff_html}</div>", unsafe_allow_html=True)
                st.caption("Red = Mutation (Danger) | Green = Restoration (Fix)")

# ==============================================================================
# MODE 2: BATCH PROCESSOR
# ==============================================================================
elif app_mode == "Batch Processor":
    st.subheader("📚 Sequential Batch Processor")

    # 1. Inputs & Navigation
    c_input, c_path, c_nav = st.columns([2, 3, 2])
    with c_input:
        st.session_state.batch_title = st.text_input("Page Title", value=st.session_state.batch_title, placeholder="e.g. Child's Way")
    with c_path:
        pdf_folder = st.text_input("PDF Folder Path", value=st.session_state.last_pdf_path)
        if pdf_folder: st.session_state.last_pdf_path = pdf_folder
    with c_nav:
        c_prev, c_pg, c_next = st.columns([1, 1, 1])
        with c_prev:
            if st.button("◀", width='stretch'):
                st.session_state.batch_page_num = max(1, st.session_state.batch_page_num - 1)
                st.session_state.gemini_result = None
                st.rerun()
        with c_pg:
            st.session_state.batch_page_num = st.number_input("Page", min_value=1, value=st.session_state.batch_page_num, label_visibility="collapsed")
        with c_next:
            if st.button("▶", width='stretch'):
                st.session_state.batch_page_num += 1
                st.session_state.gemini_result = None
                st.rerun()

    # 2. Logic: Title -> ID -> XML -> Content
    if st.session_state.batch_title and pdf_folder:
        st.divider()
        
        # Cache handling: Only re-fetch XML if the title changed
        if st.session_state.batch_cached_title != st.session_state.batch_title:
            with st.spinner(f"Resolving '{st.session_state.batch_title}'..."):
                # A. Get ID from API
                page_id = get_page_id(st.session_state.batch_title)
                
                if not page_id:
                    st.error(f"Could not find Page ID for '{st.session_state.batch_title}'")
                    st.stop()
                
                # B. Get Text from XML
                xml_path, _ = get_default_xml_path()
                st.caption(f"Fetching XML content for Page ID: {page_id}")
                wikitext, error = fetch_from_xml(xml_path, page_id)
                
                if error:
                    st.error(error)
                    st.stop()
                    
                st.session_state.batch_cached_text = wikitext
                st.session_state.batch_cached_title = st.session_state.batch_title
        
        # Use Cached Text
        wikitext = st.session_state.batch_cached_text
        
        # C. Extract Content for Current Page Number
        original_content, start_idx, end_idx, page_tag = extract_page_content_by_tag(wikitext, st.session_state.batch_page_num)

        if not page_tag:
            st.info(f"Page tag `{{{{page|{st.session_state.batch_page_num}|...}}}}` not found.")
            st.text_area("Raw Text Dump", wikitext[:1000] + "...", height=200)
        else:
            # D. Parse PDF Mapping
            file_match = re.search(r'file=([^|]+)', page_tag)
            filename = file_match.group(1).strip() if file_match else f"{st.session_state.batch_title}.pdf"
            
            pdf_idx_match = re.search(r'\|page=(\d+)', page_tag)
            target_pdf_page = int(pdf_idx_match.group(1)) if pdf_idx_match else st.session_state.batch_page_num

            st.caption(f"Editing **Page {st.session_state.batch_page_num}** | File: `{filename}` (Pg {target_pdf_page})")

            # E. Load Image
            img, error = get_page_image(pdf_folder, filename, target_pdf_page)

            # F. Workbench UI
            if st.session_state.gemini_result is None:
                # Pre-Run
                bc1, bc2 = st.columns([1, 1])
                with bc1:
                    if img: st.image(img, width='stretch')
                    elif error: st.error(error)
                with bc2:
                    st.text_area("Current Content", original_content, height=600, disabled=True)
                    if img:
                        if st.button("✨ Run Gemini", type="primary", width='stretch'):
                            with st.spinner("Reading..."):
                                st.session_state.gemini_result = proofread_page(img)
                            st.rerun()
            else:
                # Post-Run
                bc_img, bc_edit, bc_diff = st.columns([1, 1, 1])
                
                with bc_img: 
                    if img: st.image(img, width='stretch')
                
                with bc_edit:
                    final_text = st.text_area("Final Result", value=st.session_state.gemini_result, height=800)
                    
                    bsave, bskip = st.columns([1,1])
                    with bsave:
                        if st.button("💾 Save & Next", type="primary", use_container_width=True):
                            # 1. Fetch FRESH content right before saving
                            live_text = fetch_live_wikitext(st.session_state.batch_title)
                            
                            if not live_text:
                                st.error("CRITICAL: Could not fetch live page. Save aborted to prevent data loss.")
                            else:
                                # 2. Find the injection point in the LIVE text (not the cached XML)
                                _, start, end, _ = extract_page_content_by_tag(live_text, st.session_state.batch_page_num)
                                
                                if start == 0 and end == 0:
                                    st.error(f"Could not find Page {st.session_state.batch_page_num} tag in the live text.")
                                else:
                                    # 3. Surgical Splice
                                    new_wikitext = live_text[:start] + "\n" + final_text.strip() + "\n" + live_text[end:]
                                    
                                    summary = f"Batch Proofread Pg {st.session_state.batch_page_num}"
                                    res = upload_to_bahaiworks(st.session_state.batch_title, new_wikitext, summary)
                                    
                                    if res.get('edit', {}).get('result') == 'Success':
                                        st.success("Saved!")
                                        # Update cache with the result we just pushed, so the UI stays consistent
                                        st.session_state.batch_cached_text = new_wikitext 
                                        st.session_state.batch_page_num += 1
                                        st.session_state.gemini_result = None
                                        st.rerun()
                                    else:
                                        st.error(f"Save failed: {res}")
                    
                    with bskip:
                            if st.button("Skip (Next Pg)", width='stretch'):
                                st.session_state.batch_page_num += 1
                                st.session_state.gemini_result = None
                                st.rerun()

                with bc_diff:
                    diff_html = generate_smart_diff(original_content, st.session_state.gemini_result)
                    st.markdown(f"<div style='border:1px solid #ddd; padding:15px; height:800px; overflow-y:scroll; background:white; color:black;'>{diff_html}</div>", unsafe_allow_html=True)
