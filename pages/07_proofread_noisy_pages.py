import streamlit as st
import pandas as pd
import difflib
import re
import os
import sys
import io
import urllib.parse
import requests
import sqlite3
from PIL import Image
import fitz  # PyMuPDF
import google.generativeai as genai

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
                a.title,
                a.source_code,
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

def extract_page_content_by_tag(wikitext: str, pdf_page_num: int):
    """
    Parses live wikitext to find the content between {{page|...|page=X}} tags.
    Returns: (text_content, start_index, end_index, full_template_string)
    """
    # Regex to match {{page|LABEL|file=...|page=NUMBER}}
    # We look for the specific pdf_page_num
    pattern = re.compile(r'(\{\{page\|[^|]*\|file=[^|]+\|page=' + str(pdf_page_num) + r'\}\})')
    match = pattern.search(wikitext)
    
    if not match:
        return None, 0, 0, None
    
    start_tag_end = match.end()
    start_tag_full = match.group(1)
    
    # Find the NEXT page tag to define the end of this page
    next_tag_pattern = re.compile(r'\{\{page\|')
    next_match = next_tag_pattern.search(wikitext, start_tag_end)
    
    if next_match:
        end_index = next_match.start()
    else:
        end_index = len(wikitext) # End of file
        
    content = wikitext[start_tag_end:end_index]
    return content, start_tag_end, end_index, start_tag_full

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

def proofread_with_gemini(image):
    model = genai.GenerativeModel('gemini-pro-vision')
    prompt = """
    You are a strict archival transcription engine. 
    1. Transcribe the text from this page image character-for-character.
    2. Do NOT correct grammar or modernization spelling.
    3. If the text has an OBVIOUS typo (e.g. "sentance"), transcribe it as: {{sic|sentance|sentence}}
    4. Preserve paragraph breaks.
    5. Return ONLY the text. No markdown formatting blocks (```), no conversational filler.
    """
    try:
        response = model.generate_content([prompt, image])
        return response.text.strip()
    except Exception as e:
        return f"Error: {e}"

def get_live_wikitext(title):
    session = requests.Session()
    params = {
        "action": "query", "prop": "revisions", "titles": title,
        "rvprop": "content", "format": "json", "rvslots": "main"
    }
    try:
        data = session.get(API_URL, params=params).json()
        pages = data['query']['pages']
        for pid in pages:
            if pid != "-1":
                return pages[pid]['revisions'][0]['slots']['main']['*']
    except Exception as e:
        st.error(f"Wiki API Error: {e}")
    return None

# ==============================================================================
# 4. UI LAYOUT
# ==============================================================================

# --- Sidebar Controls ---
with st.sidebar:
    st.header("🔍 Discovery Filters")
    min_noise = st.slider("Min Noise Score", 0, 100, 30)
    
    # Store PDF path in session to persist across reruns
    if 'pdf_root' not in st.session_state:
        st.session_state.pdf_root = "/home/kubuntu/pdfs"
    
    pdf_root = st.text_input("PDF Folder Path", value=st.session_state.pdf_root)
    st.session_state.pdf_root = pdf_root
    
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
    
    # 1. Fetch Data
    with st.spinner("Fetching live Wiki content & PDF Image..."):
        wikitext = get_live_wikitext(row['title'])
        if not wikitext:
            st.error("Could not fetch wikitext.")
            st.stop()
            
        # Extract the specific page content from the huge wikitext file
        original_content, start_idx, end_idx, page_tag = extract_page_content_by_tag(wikitext, row['physical_page_number'])
        
        if not page_tag:
            st.warning(f"Could not find a {{page}} tag for physical page {row['physical_page_number']} in the live text.")
            st.code(wikitext[:500], language='text')
            st.stop()
            
        # Parse filename from tag 
        # Tag format: {{page|VII|file=Filename.pdf|page=10}}
        file_match = re.search(r'file=([^|]+)', page_tag)
        if file_match:
            filename = file_match.group(1).strip()
        else:
            filename = f"{row['title']}.pdf" 
        
        # Get Image
        img, error = get_page_image(st.session_state.pdf_root, filename, row['physical_page_number'])

    # 2. Two-Column Layout
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        st.subheader("Source PDF")
        if img:
            st.image(img, use_column_width=True)
        else:
            st.error(f"Image Error: {error}")
            st.warning("Ensure the PDF path in the sidebar is correct.")
            
    with col_right:
        st.subheader("Smart Diff")
        
        if st.session_state.gemini_result is None:
            st.info("Original Text (High Noise detected)")
            st.text_area("Original", original_content, height=300, disabled=True)
            
            if img and st.button("✨ Run Gemini OCR", type="primary"):
                with st.spinner("Gemini is reading..."):
                    st.session_state.gemini_result = proofread_with_gemini(img)
                st.rerun()
        else:
            # RENDER SMART DIFF
            diff_html = generate_smart_diff(original_content, st.session_state.gemini_result)
            
            st.markdown(
                f"""
                <div style="
                    border:1px solid #ddd; 
                    padding:15px; 
                    height:400px; 
                    overflow-y:scroll; 
                    font-family:monospace; 
                    white-space: pre-wrap;
                    background-color: white;
                    color: black;
                ">
                    {diff_html}
                </div>
                <div style="margin-top:5px; font-size:0.8em; color:gray;">
                    <span style="background-color:#ffcccc; padding:0 5px;">Red</span> = Mutation (Original was clean) | 
                    <span style="background-color:#e6ffe6; padding:0 5px;">Green</span> = Restoration (Original was noise)
                </div>
                """, 
                unsafe_allow_html=True
            )
            
            st.divider()
            
            # EDITABLE FINAL RESULT
            final_text = st.text_area("Final Text (Edit before saving)", value=st.session_state.gemini_result, height=200)
            
            if st.button("💾 Save to Bahai.works"):
                # Construct new wikitext
                new_wikitext = wikitext[:start_idx] + "\n" + final_text.strip() + "\n" + wikitext[end_idx:]
                
                summary = f"Proofread Pg {row['physical_page_number']} via Dashboard (Smart Diff)."
                res = upload_to_bahaiworks(row['title'], new_wikitext, summary)
                
                if res.get('edit', {}).get('result') == 'Success':
                    st.success("Saved!")
                    # Clear state to force refresh
                    st.session_state.gemini_result = None
                    st.session_state.current_selection = None
                    # Update queue to remove fixed item locally (optional but nice)
                    st.session_state.queue_df = get_noisy_pages_from_db(min_noise)
                    st.rerun()
                else:
                    st.error(f"Save failed: {res}")
            
            if st.button("Discard & Retry"):
                st.session_state.gemini_result = None
                st.rerun()
