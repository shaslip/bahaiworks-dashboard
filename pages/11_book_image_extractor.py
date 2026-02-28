import streamlit as st
import os
import sys
import re
import cv2
import numpy as np
from pdf2image import convert_from_path

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.gemini_processor import parse_range_string, extract_image_caption_and_filename

st.set_page_config(page_title="Book Image Extractor", page_icon="🖼️", layout="wide")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def find_local_pdf(filename, root_folder):
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower():
                return os.path.join(dirpath, f)
    return None

def crop_illustrations(pil_img, expected_count=1):
    """Uses OpenCV to find contours and crop out the illustrations. Returns a list of cropped images."""
    img = np.array(pil_img)
    img = img[:, :, ::-1].copy() 
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Invert the image
    _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
    
    # Dilate to group parts of the illustration together
    kernel = np.ones((5,5), np.uint8)
    dilated = cv2.dilate(thresh, kernel, iterations=2)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return []
        
    # Sort by area descending and take the top `expected_count` contours
    sorted_by_area = sorted(contours, key=cv2.contourArea, reverse=True)
    top_contours = sorted_by_area[:expected_count]
    
    # Sort those top contours from top-to-bottom so they match Gemini's reading order
    top_contours = sorted(top_contours, key=lambda c: cv2.boundingRect(c)[1])
    
    cropped_images = []
    for c in top_contours:
        x, y, w, h = cv2.boundingRect(c)
        # Crop exactly to the bounding box, zero buffer
        cropped = img[y:y+h, x:x+w]
        cropped_images.append(cropped)
        
    return cropped_images

def create_wiki_text_file(txt_path, caption, book_title, access_control=""):
    clean_title = re.sub(r'\.pdf$', '', book_title, flags=re.IGNORECASE).replace('_', ' ')
    
    # Add a newline after the tag if it exists, otherwise leave blank
    access_block = f"{access_control.strip()}\n" if access_control.strip() else ""
    
    content = f"""{access_block}== File info ==
{{{{cs
| caption = {caption}
| source = {clean_title}
}}}}

[[Category:{clean_title}]]
"""
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)

# ==============================================================================
# UI & MAIN LOGIC
# ==============================================================================

st.title("🖼️ Book Image Extractor")

st.sidebar.header("Configuration")
input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")

pdf_filename = st.text_input("PDF Filename", placeholder="e.g., A_Day_for_Very_Great_Things.pdf")
page_ranges = st.text_input("Page Ranges", placeholder="e.g., 527-546, 12, 15-20")
access_control = st.text_input("Access Control (Optional)", placeholder="e.g., <accesscontrol>Access:DayVeryGreatThings</accesscontrol>")

if st.button("🚀 Process Images", type="primary"):
    if not pdf_filename or not page_ranges:
        st.warning("Please provide both a PDF filename and page ranges.")
        st.stop()
        
    local_pdf_path = find_local_pdf(pdf_filename, input_folder)
    
    if not local_pdf_path:
        st.error(f"❌ Could not find {pdf_filename} in {input_folder}")
        st.stop()
        
    pages_to_process = parse_range_string(page_ranges)
    if not pages_to_process:
        st.warning("No valid pages found in range.")
        st.stop()
        
    pdf_dir = os.path.dirname(local_pdf_path)
    clean_pdf_name = re.sub(r'\.pdf$', '', pdf_filename, flags=re.IGNORECASE)
    output_dir = os.path.join(pdf_dir, "images", clean_pdf_name)
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    st.info(f"📂 Output directory: {output_dir}")
        
    progress_bar = st.progress(0)
    status_text = st.empty()
    log_container = st.container(border=True)
    
    for idx, page_num in enumerate(pages_to_process):
        status_text.markdown(f"**Processing Page {page_num} ({idx+1}/{len(pages_to_process)})...**")
        
        # 1. Extract Page using pdf2image
        log_container.write(f"📄 Extracting page {page_num}...")
        try:
            images = convert_from_path(local_pdf_path, first_page=page_num, last_page=page_num, dpi=300)
            if not images:
                log_container.warning(f"⚠️ Could not extract page {page_num}. Skipping.")
                continue
            pil_img = images[0]
        except Exception as e:
            log_container.error(f"❌ Error converting page {page_num}: {e}")
            continue
            
        # 2. Ask Gemini for Captions and Filenames
        log_container.write("🧠 Requesting captions and filenames from Gemini...")
        gemini_data_list = extract_image_caption_and_filename(pil_img, default_name=f"page_{page_num}_image.png")
        
        if not gemini_data_list:
            log_container.warning(f"⚠️ No images detected by Gemini on page {page_num}. Skipping.")
            continue
            
        # 3. Crop Illustrations using OpenCV
        log_container.write(f"✂️ Auto-cropping {len(gemini_data_list)} image(s) from page...")
        cropped_cv2_images = crop_illustrations(pil_img, expected_count=len(gemini_data_list))
        
        for i, img_data in enumerate(gemini_data_list):
            caption = img_data.get("caption", "")
            proposed_filename = img_data.get("filename", f"page_{page_num}_image_{i+1}.png")
            
            # Ensure unique filename
            final_img_path = os.path.join(output_dir, proposed_filename)
            counter = 1
            while os.path.exists(final_img_path):
                name, ext = os.path.splitext(proposed_filename)
                final_img_path = os.path.join(output_dir, f"{name}_{counter}{ext}")
                counter += 1
                
            final_filename = os.path.basename(final_img_path)
            final_txt_path = os.path.join(output_dir, final_filename.replace(".png", ".txt"))
            
            # Match the cropped image to the Gemini data (fallback to full page if crop fails)
            if i < len(cropped_cv2_images):
                cv2.imwrite(final_img_path, cropped_cv2_images[i])
            else:
                log_container.warning(f"⚠️ Could not auto-crop image {i+1}. Saving uncropped page.")
                pil_img.save(final_img_path) 
                
            # 4. Generate .txt file
            log_container.write(f"📝 Generating MediaWiki text file for {final_filename}...")
            create_wiki_text_file(final_txt_path, caption, clean_pdf_name, access_control)
            
            log_container.success(f"✅ Finished page {page_num}, image {i+1} -> Saved as `{final_filename}`")
            
        progress_bar.progress((idx + 1) / len(pages_to_process))
        log_container.success(f"✅ Finished page {page_num} -> Saved as `{final_filename}`")
        
    status_text.success("🎉 All images extracted and processed successfully!")
