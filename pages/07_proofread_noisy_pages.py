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

def get_live_wikitext(title):
    print(f"DEBUG: Fetching wikitext for '{title}'...")
    session = requests.Session()
    params = {
        "action": "query", "prop": "revisions", "titles": title,
        "rvprop": "content", "format": "json", "rvslots": "main"
    }
    try:
        # Added 10s timeout to prevent infinite hangs
        response = session.get(API_URL, params=params, timeout=10)
        data = response.json()
        print("DEBUG: Wiki response received.")
        
        pages = data['query']['pages']
        for pid in pages:
            if pid != "-1":
                text = pages[pid]['revisions'][0]['slots']['main']['*']
                print(f"DEBUG: Content extracted ({len(text)} chars).")
                return text
    except Exception as e:
        print(f"ERROR: Wiki API failed: {e}")
        st.error(f"Wiki API Error: {e}")
    return None

# ==============================================================================
# 4. UI LAYOUT
# ==============================================================================

# --- Sidebar Controls ---
with st.sidebar:
    st.header("🔍 Discovery Filters")
    min_noise = st.slider("Min Noise Score", 0, 100, 30)

    if st.button("Refresh Queue"):
        st.session_state.queue_df = get_noisy_pages_from_db(min_noise)
        st.session_state.current_selection = None
        st.session_state.gemini_result = None

# --- State Init ---
if 'queue_df' not in st.session_state:
    st.session_state.queue_df = get_noisy_pages_from_db(min_noise)
if 'current_selection' not in st.session_state:
    st.session_state.current_selection = None
if 'gemini_result' not in st.session_state:
    st.session_state.gemini_result = None

# --- Main View ---
st.title("🛡️ Noisy Page Proofreader")

# TAB 1: THE QUEUE
if st.session_state.current_selection is None:
    st.markdown(f"### High Noise Pages ({len(st.session_state.queue_df)})")
    
    if st.session_state.queue_df.empty:
        st.success("No pages found above the noise threshold.")
    else:
        # Display as a selectable dataframe
        # We iterate to create buttons for selection
        for idx, row in st.session_state.queue_df.iterrows():
            with st.container(border=True):
                # Header Row: Title Link | Metrics | Fix Button
                c1, c2, c3 = st.columns([6, 2, 1])
                
                url = generate_bahai_works_url(row['title'], row['physical_page_number'])
                
                with c1:
                    st.markdown(f"### [{row['title']} (Pg {row['physical_page_number']})]({url})")
                    st.caption(f"Source: {row['source_code'].upper()}")

                with c2:
                    st.metric("Max Noise", f"{row['max_seg_noise']:.1f}")

                with c3:
                    if st.button("🛠️ Fix", key=f"btn_{idx}", use_container_width=True):
                        st.session_state.current_selection = row
                        st.rerun()

                # Context Row: The "Garbage" Text
                # Truncate if insanely long, but usually segments are <1000 chars
                st.text_area(
                    label="Preview of highest noise segment", 
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
    
    # --- STEP 1: Fetch Wiki Content (Local XML) ---
    target_pdf_page = None
    wikitext = None

    with st.status("Loading Page Context (Local)...", expanded=True) as status:
        # Auto-detect XML path
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
            
        st.write(f"🔍 Searching for Physical Page {row['physical_page_number']} tag...")
        
        original_content, start_idx, end_idx, page_tag = extract_page_content_by_tag(wikitext, row['physical_page_number'])
        
        if not page_tag:
            status.update(label="Tag Search Failed", state="error")
            st.error(f"Could not find `{{{{page|{row['physical_page_number']}|...}}}}` tag in text.")
            st.stop()
        
        # Parse Metadata
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

    # --- STEP 2: Ask for PDF Path (Interactive) ---
    st.info(f"Target PDF: **{filename}**")
    
    # Initialize session state for path if missing
    if 'last_pdf_path' not in st.session_state: st.session_state.last_pdf_path = ""
    
    # User Input - NOT inside a spinner
    pdf_folder = st.text_input("Enter folder path for this PDF:", value=st.session_state.last_pdf_path)
    
    img = None
    error = None

    # --- STEP 3: Fetch Image (Only if path exists) ---
    if pdf_folder:
        st.session_state.last_pdf_path = pdf_folder # Save for next time
        
        with st.spinner("Extracting Page Image..."):
            # Use target_pdf_page (e.g. 21) instead of row['physical_page_number'] (e.g. 19)
            img, error = get_page_image(pdf_folder, filename, target_pdf_page)
            
        if error:
            st.error(f"Could not load PDF: {error}")
    else:
        st.warning("☝️ Please enter the folder path above to load the PDF.")

    # --- UI: Two-Column Layout ---
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.subheader("Source PDF")
        if img: st.image(img, width='stretch')
            
    with col_right:
        st.subheader("Smart Diff")
        
        if st.session_state.gemini_result is None:
            st.text_area("Original Text", original_content, height=300, disabled=True)
            
            # Only show run button if we actually have an image
            if img:
            if st.button("✨ Run Gemini OCR", type="primary"):
                with st.spinner("Gemini is reading..."):
                    # Changed from proofread_with_gemini(img)
                    st.session_state.gemini_result = proofread_page(img)
                st.rerun()
        else:
            diff_html = generate_smart_diff(original_content, st.session_state.gemini_result)
            st.markdown(f"<div style='border:1px solid #ddd; padding:15px; height:400px; overflow-y:scroll; font-family:monospace; background-color:white; color:black;'>{diff_html}</div>", unsafe_allow_html=True)
            st.caption("Red = Mutation (Danger) | Green = Restoration (Fix)")
            
            final_text = st.text_area("Final Text", value=st.session_state.gemini_result, height=200)
            
            c_save, c_discard = st.columns([1,1])
            with c_save:
                if st.button("💾 Save to Bahai.works", type="primary"):
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
                if st.button("Discard"):
                    st.session_state.gemini_result = None
                    st.rerun()
