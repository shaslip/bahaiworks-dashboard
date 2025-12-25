import streamlit as st
import os
import re
from PIL import Image
from sqlalchemy.orm import Session
from sqlalchemy import select
from streamlit_cropper import st_cropper

# Local imports
from src.database import engine, Document

st.set_page_config(page_title="Image Import", layout="wide")
st.title("🖼️ Image Import & Processing")

# --- HELPERS ---

def get_candidate_books():
    """Finds digitized books that have marked illustrations."""
    with Session(engine) as session:
        docs = session.scalars(
            select(Document).where(
                Document.status == "DIGITIZED",
                Document.ai_justification.contains("[RANGES:")
            )
        ).all()
    return docs

def get_ocr_snippet(doc, page_num):
    """
    Reads the main text file and attempts to find content for {{page|illus.X}}
    We have to guess the 'illus.X' number or search by raw page index? 
    The OCR engine labeled them sequentially as illus.1, illus.2...
    
    Since we only have page number in filename (illus_p55.png), we might need to 
    scan the text file for the tag that corresponds to page 55.
    
    Tag format: {{page|illus.1|file=Book.pdf|page=55}}
    """
    txt_path = doc.file_path.replace(".pdf", ".txt")
    if not os.path.exists(txt_path):
        return "Text file not found."
    
    try:
        with open(txt_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Look for the tag: page=55
        # Regex: {{page|([^|]+)|.*?page=55}}
        pattern = rf"{{{{page\|([^|]+)\|.*?page={page_num}}}}}(.*?)(?={{{{page\||$)"
        match = re.search(pattern, content, re.DOTALL)
        
        if match:
            label = match.group(1) # e.g. "illus.1"
            text_body = match.group(2).strip()
            return f"[{label}] {text_body}"
        return "Caption not found."
    except Exception as e:
        return f"Error reading text: {e}"

# --- MAIN UI ---

st.sidebar.header("Select Book")
candidates = get_candidate_books()

if not candidates:
    st.info("No books with illustrations found.")
    st.stop()

format_func = lambda d: f"{d.filename}"
selected_doc = st.sidebar.selectbox("Choose a book:", candidates, format_func=format_func)

if selected_doc:
    # 1. Locate the Images Folder
    # Matches logic in src/ocr_engine.py cleanup()
    work_dir = os.path.dirname(selected_doc.file_path)
    book_name = os.path.splitext(selected_doc.filename)[0]
    
    raw_dir = os.path.join(work_dir, "images", book_name, "raw")
    processed_dir = os.path.join(work_dir, "images", book_name, "processed")
    os.makedirs(processed_dir, exist_ok=True)
    
    if not os.path.exists(raw_dir) or not os.listdir(raw_dir):
        st.warning(f"No extracted images found at {raw_dir}.")
        st.caption("Did you run the OCR Batch process (Step 3) for this file yet?")
        st.stop()

    # 2. File Selection
    files = sorted([f for f in os.listdir(raw_dir) if f.endswith(".png")])
    
    col_nav1, col_nav2 = st.columns([1, 3])
    with col_nav1:
        selected_file = st.selectbox("Select Image:", files)
        
        # Extract page number for OCR lookup
        # Filename format from engine: "illus_p55.png"
        try:
            page_num = int(re.search(r'p(\d+)', selected_file).group(1))
        except:
            page_num = 0

    img_path = os.path.join(raw_dir, selected_file)
    original_image = Image.open(img_path)

    # 3. Work Area
    c1, c2 = st.columns([2, 1])
    
    with c1:
        st.subheader("✂️ Crop")
        cropped_img = st_cropper(original_image, realtime_update=True, box_color='#FF0000', aspect_ratio=None)
        st.caption("Preview:")
        st.image(cropped_img, width=250)

    with c2:
        st.subheader("📝 Metadata")
        
        # Auto-fetch caption
        raw_text = get_ocr_snippet(selected_doc, page_num)
        
        with st.form("save_image"):
            new_name = st.text_input("Filename", value=f"{book_name}_p{page_num}.jpg")
            caption = st.text_area("Caption", value=raw_text, height=200)
            
            if st.form_submit_button("💾 Save Processed"):
                save_path = os.path.join(processed_dir, new_name)
                if cropped_img.mode != "RGB":
                    cropped_img = cropped_img.convert("RGB")
                cropped_img.save(save_path, quality=90)
                st.success(f"Saved to {save_path}")
                st.toast("Saved!")

    # 4. Gallery
    st.divider()
    st.subheader("Processed Images")
    if os.path.exists(processed_dir):
        p_files = os.listdir(processed_dir)
        if p_files:
            cols = st.columns(6)
            for i, f in enumerate(p_files):
                with cols[i % 6]:
                    st.image(os.path.join(processed_dir, f), caption=f)
