import streamlit as st
import xml.etree.ElementTree as ET
import re
import os
import sys
import requests
from PIL import Image
import io
import fitz  # PyMuPDF
import google.generativeai as genai

# --- Setup Project Path ---
# This allows the script to find modules in the 'src' directory
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.append(PROJECT_ROOT)
from src.mediawiki_uploader import upload_to_bahaiworks, API_URL, get_csrf_token
# --- End Setup ---

# --- Gemini API Configuration ---
# Ensure GEMINI_API_KEY is set in your .env file
if 'GEMINI_API_KEY' not in os.environ:
    st.error("GEMINI_API_KEY not found in environment variables. Please check your .env file.")
    st.stop()
genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# --- Page Configuration ---
st.set_page_config(
    page_title="Noisy Page Proofreader",
    page_icon="🔎",
    layout="wide"
)
st.title("🔎 Noisy Page Proofreader")
st.write(
    "This tool analyzes a bahai.works XML dump to find pages with a high ratio of 'noise' "
    "(likely from OCR errors). It then allows you to proofread the page against the original PDF "
    "using Gemini and update the wiki directly."
)

# --- Helper Functions ---

def calculate_noise(text: str) -> float:
    """Calculates a 'noise' score for a given text."""
    if not text:
        return 0.0
    # A simple heuristic: count characters that are NOT standard letters, numbers,
    # whitespace, or common punctuation. This is effective at catching OCR gibberish.
    allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n\t .,!?'\"()[]-")
    noise_chars = sum(1 for char in text if char not in allowed_chars)
    
    # Return noise as a percentage of total characters
    return (noise_chars / len(text)) * 100 if len(text) > 0 else 0

@st.cache_data(show_spinner="Parsing XML dump...")
def parse_xml_dump(uploaded_file):
    """Parses the XML dump and returns a sorted list of pages by noise score."""
    noisy_pages = []
    # Use iterparse for memory efficiency with large XML files
    context = ET.iterparse(uploaded_file, events=('end',))
    for _, elem in context:
        if elem.tag.endswith('page'):
            title_elem = elem.find('{*}title')
            text_elem = elem.find('{*}revision/{*}text')
            
            if title_elem is not None and text_elem is not None and text_elem.text:
                title = title_elem.text
                text = text_elem.text
                noise_score = calculate_noise(text)
                
                # We only care about pages with potential issues and the {{page}} template
                if noise_score > 5.0 and '{{page|' in text:
                    noisy_pages.append({
                        'title': title,
                        'score': noise_score,
                        'text': text
                    })
            # Clear the element to free up memory
            elem.clear()
            
    return sorted(noisy_pages, key=lambda x: x['score'], reverse=True)

def extract_page_info(wikitext: str):
    """Finds all {{page}} templates and extracts their data."""
    # Regex to find {{page|...}} and capture file and page parameters
    pattern = r"\{\{page\|[^}]*?file=([^|}]+)[^}]*?page=(\d+)[^}]*?\}\}"
    matches = re.finditer(pattern, wikitext)
    
    page_data = []
    for match in matches:
        page_data.append({
            'filename': match.group(1).strip(),
            'pdf_page': int(match.group(2).strip()),
            'full_tag': match.group(0),
            'start_pos': match.start(),
            'end_pos': match.end()
        })
    return page_data

@st.cache_data(show_spinner="Extracting page image from PDF...")
def get_page_as_image(pdf_path: str, page_num: int) -> Image.Image:
    """Extracts a single page from a PDF and returns it as a PIL Image object."""
    try:
        doc = fitz.open(pdf_path)
        # page_num from template is 1-based, PyMuPDF is 0-based
        page = doc.load_page(page_num - 1) 
        # Use 2x zoom (matrix) for higher resolution, improving OCR/Gemini accuracy
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        img_data = pix.tobytes("png")
        image = Image.open(io.BytesIO(img_data))
        doc.close()
        return image
    except Exception as e:
        st.error(f"Failed to extract page {page_num} from {os.path.basename(pdf_path)}: {e}")
        return None

@st.cache_data(show_spinner="Asking Gemini to proofread the page...")
def proofread_page_image(image: Image.Image) -> str:
    """Sends a page image to Gemini for high-fidelity transcription."""
    model = genai.GenerativeModel('gemini-pro-vision')

    prompt = """
    You are a meticulous archivist. Your task is to transcribe the text from the following page image exactly as it appears.

    - Transcribe every word, including headers, footers, and page numbers.
    - Preserve original paragraph breaks.
    - If you encounter italicized text, wrap it in ''double single quotes'' for MediaWiki formatting.
    - If a word is clearly unreadable or smudged, represent it as [unreadable].
    - Do not add any commentary, explanation, or greetings. Return ONLY the transcribed text.
    """

    try:
        response = model.generate_content([prompt, image])
        return response.text.strip()
    except Exception as e:
        return f"Error during Gemini transcription: {e}"

def get_page_content(title: str) -> str:
    """Fetches the current wikitext of a page."""
    session = requests.Session()
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": title,
        "rvprop": "content",
        "format": "json",
        "rvslots": "main"
    }
    response = session.get(API_URL, params=params)
    data = response.json()
    pages = data['query']['pages']
    for page_id in pages:
        if page_id != "-1":
            return pages[page_id]['revisions'][0]['slots']['main']['*']
    return None

# --- Main App Logic ---

# Initialize session state
if 'noisy_pages' not in st.session_state:
    st.session_state.noisy_pages = None
if 'selected_page' not in st.session_state:
    st.session_state.selected_page = None
if 'gemini_text' not in st.session_state:
    st.session_state.gemini_text = None

# --- View 1: File Upload and Analysis ---
if st.session_state.selected_page is None:
    st.header("1. Analyze Wiki Dump")
    
    xml_file = st.file_uploader("Upload bahai.works XML dump", type=['xml'])
    pdf_folder = st.text_input("Enter the absolute path to the folder containing source PDFs")

    if st.button("Analyze XML Dump", disabled=(not xml_file or not pdf_folder)):
        st.session_state.noisy_pages = parse_xml_dump(xml_file)
        st.session_state.pdf_folder = pdf_folder
        if not st.session_state.noisy_pages:
            st.warning("No pages with high noise and a `{{page}}` template were found.")

    if st.session_state.noisy_pages:
        st.header("2. Select a Page to Proofread")
        st.write(f"Found **{len(st.session_state.noisy_pages)}** potential pages to fix.")
        
        for i, page_data in enumerate(st.session_state.noisy_pages):
            with st.expander(f"**{page_data['title']}** (Noise Score: {page_data['score']:.2f})"):
                # Show a snippet of the text
                snippet = (page_data['text'][:300] + '...') if len(page_data['text']) > 300 else page_data['text']
                st.code(snippet, language='text')
                
                if st.button("Proofread This Page", key=f"proof_{i}"):
                    st.session_state.selected_page = page_data
                    st.session_state.gemini_text = None # Reset gemini text
                    st.rerun()

# --- View 2: Proofreading Interface ---
else:
    page_data = st.session_state.selected_page
    st.header(f"Proofreading: `{page_data['title']}`")
    
    if st.button("← Back to List"):
        st.session_state.selected_page = None
        st.session_state.gemini_text = None
        st.rerun()

    # Step 1: Extract template info from the text
    page_templates = extract_page_info(page_data['text'])
    
    if not page_templates:
        st.error("Could not find a valid `{{page|...}}` template in the wikitext.")
        st.stop()

    # We will work with the first template on the page
    template = page_templates[0]
    pdf_filename = template['filename']
    pdf_page_num = template['pdf_page']
    
    st.info(f"Identified request: **File:** `{pdf_filename}` | **Page:** `{pdf_page_num}`")

    # Step 2: Locate file and extract image
    pdf_full_path = os.path.join(st.session_state.pdf_folder, pdf_filename)
    if not os.path.exists(pdf_full_path):
        st.error(f"PDF file not found at the specified path: `{pdf_full_path}`")
        st.stop()
        
    page_image = get_page_as_image(pdf_full_path, pdf_page_num)

    if page_image:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Original Page Image")
            st.image(page_image, use_column_width=True)
        
        with col2:
            st.subheader("Text Content")
            
            # Find the text snippet to be replaced
            start_replace_idx = template['end_pos']
            # If there's a next page tag, end there. Otherwise, end at file end.
            end_replace_idx = page_templates[1]['start_pos'] if len(page_templates) > 1 else len(page_data['text'])
            original_text_snippet = page_data['text'][start_replace_idx:end_replace_idx].strip()
            
            st.write("Current OCR Text:")
            st.text_area("Original", value=original_text_snippet, height=200, disabled=True)

            if st.button("✨ Proofread with Gemini"):
                st.session_state.gemini_text = proofread_page_image(page_image)
                st.rerun()

            if st.session_state.gemini_text:
                st.session_state.gemini_text = st.text_area("Gemini's Proofread Text (Editable)", value=st.session_state.gemini_text, height=300)

                if st.button("✅ Update bahai.works", type="primary"):
                    with st.spinner("Updating wiki page..."):
                        # Fetch the absolute latest version of the page before editing
                        live_wikitext = get_page_content(page_data['title'])
                        if not live_wikitext:
                            st.error("Failed to fetch the latest version of the page. Aborting update.")
                            st.stop()

                        # Re-run template extraction on the live text to get fresh positions
                        live_templates = extract_page_info(live_wikitext)
                        if not live_templates:
                            st.error("Live page content is missing the template. Aborting.")
                            st.stop()
                        
                        live_template_one = live_templates[0]
                        
                        start_idx = live_template_one['end_pos']
                        end_idx = live_templates[1]['start_pos'] if len(live_templates) > 1 else len(live_wikitext)

                        # Construct the new page content
                        new_content = (
                            live_wikitext[:start_idx].strip() +
                            "\n" + st.session_state.gemini_text.strip() + "\n" +
                            live_wikitext[end_idx:].strip()
                        )
                        
                        try:
                            summary = f"Proofread page {pdf_page_num} of {pdf_filename} with Gemini assistance via Dashboard."
                            response = upload_to_bahaiworks(page_data['title'], new_content, summary)
                            if response.get('edit', {}).get('result') == 'Success':
                                st.success(f"Successfully updated page '{page_data['title']}'!")
                                # Clear state to go back to the list
                                st.session_state.selected_page = None
                                st.session_state.gemini_text = None
                                # This short sleep gives the success message time to be seen before rerun
                                import time
                                time.sleep(2)
                                st.rerun()
                            else:
                                st.error(f"API Error: {response}")
                        except Exception as e:
                            st.error(f"Failed to update page: {e}")
