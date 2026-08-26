import streamlit as st
import os
import sys
import re
import cv2
import numpy as np
from pdf2image import convert_from_path
import requests

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

@st.cache_data(ttl=3600)
def fetch_bw_offset_map(module_name):
    """Fetches and parses a simple 1D Lua offset map for Baha'i World."""
    url = "https://bahai.media/api.php"
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "titles": module_name,
        "format": "json",
        "rvslots": "main"
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        pages = data.get("query", {}).get("pages", {})
        page_id = list(pages.keys())[0]
        
        if page_id == "-1":
            return {}
            
        content = pages[page_id]["revisions"][0]["slots"]["main"]["*"]
        map_match = re.search(r'pdfOffset_map\s*=\s*\{([^}]+)\}', content)
        if not map_match:
            return {}
            
        map_str = map_match.group(1)
        offset_map = {}
        pairs = re.findall(r'\[\s*(\d+)\s*\]\s*=\s*[\'"]?(-?\d+)[\'"]?', map_str)
        for k, v in pairs:
            offset_map[int(k)] = int(v)
            
        return offset_map
    except Exception as e:
        print(f"Error fetching {module_name} offset map: {e}")
        return {}

@st.cache_data(ttl=3600)
def fetch_ab_maps(module_name):
    """Fetches both the double page map and offset map for American Baha'i."""
    url = "https://bahai.media/api.php"
    params = {
        "action": "query",
        "prop": "revisions",
        "rvprop": "content",
        "titles": module_name,
        "format": "json",
        "rvslots": "main"
    }
    try:
        response = requests.get(url, params=params)
        content = list(response.json().get("query", {}).get("pages", {}).values())[0]["revisions"][0]["slots"]["main"]["*"]
        
        dp_map = {}
        dp_match = re.search(r'pdfDoublePage_map\s*=\s*\{([^}]+)\}', content)
        if dp_match:
            pairs = re.findall(r'\[\s*(\d+)\s*\]\s*=\s*(-?\d+)', dp_match.group(1))
            dp_map = {int(k): int(v) for k, v in pairs}
            
        off_map = {}
        off_match = re.search(r'pdfOffset_map\s*=\s*\{([^}]+)\}', content)
        if off_match:
            pairs = re.findall(r'\[\s*(\d+)\s*\]\s*=\s*(-?\d+)', off_match.group(1))
            off_map = {int(k): int(v) for k, v in pairs}
            
        return dp_map, off_map
    except Exception as e:
        print(f"Error fetching {module_name} maps: {e}")
        return {}, {}

def calculate_ab_physical_page(pdf_page, vol, issue, dp_map, off_map):
    """Reverses the Lua logic to find the physical page from the PDF page."""
    # Replicate Lua string.format("%02d%02d", vol, issue)
    key = int(f"{vol:02d}{issue:02d}")
    
    double_page_val = dp_map.get(key, 0)
    base_offset = off_map.get(key, 0)
    
    # Fallback just in case of a typo in the Lua module (e.g. [351] instead of [3501])
    if base_offset == 0 and int(f"{vol}{issue}") in off_map:
        base_offset = off_map.get(int(f"{vol}{issue}"), 0)
        
    if double_page_val > 0:
        # Calculate where the double page occurs in the PDF
        threshold_pdf_page = double_page_val + base_offset
        if pdf_page <= threshold_pdf_page:
            return pdf_page - base_offset
        else:
            return pdf_page - base_offset + 1
    else:
        return pdf_page - base_offset

def find_local_pdf(filename, root_folder):
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower() == filename.lower():
                return os.path.join(dirpath, f)
    return None

def crop_illustrations(pil_img, expected_count=1):
    """Uses OpenCV to find contours and crop out the illustrations."""
    img = np.array(pil_img)
    img = img[:, :, ::-1].copy() 
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
    
    kernel = np.ones((5,5), np.uint8)
    dilated = cv2.dilate(thresh, kernel, iterations=2)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return []
        
    sorted_by_area = sorted(contours, key=cv2.contourArea, reverse=True)
    top_contours = sorted_by_area[:expected_count]
    top_contours = sorted(top_contours, key=lambda c: (cv2.boundingRect(c)[1] // 100, cv2.boundingRect(c)[0]))
    
    cropped_images = []
    for c in top_contours:
        x, y, w, h = cv2.boundingRect(c)
        rough_crop = img[y:y+h, x:x+w]
        
        gray_cropped = cv2.cvtColor(rough_crop, cv2.COLOR_BGR2GRAY)
        _, thresh_cropped = cv2.threshold(gray_cropped, 230, 255, cv2.THRESH_BINARY_INV)
        coords = cv2.findNonZero(thresh_cropped)
        
        if coords is not None:
            tx, ty, tw, th = cv2.boundingRect(coords)
            final_crop = rough_crop[ty:ty+th, tx:tx+tw]
        else:
            final_crop = rough_crop 
            
        cropped_images.append(final_crop)
        
    return cropped_images

def create_wiki_text_file(txt_path, caption, book_title, access_control="", 
                          is_bw_volume=False, bw_volume=None, 
                          is_ab_issue=False, ab_vol=None, ab_issue=None, physical_page=None):
    clean_title = re.sub(r'\.pdf$', '', book_title, flags=re.IGNORECASE).replace('_', ' ')
    access_block = f"{access_control.strip()}\n" if access_control.strip() else ""
    
    if is_bw_volume and bw_volume is not None and physical_page is not None:
        content = f"""{access_block}== File info ==
{{{{cs
| caption = {caption}
| source = {{{{bws|{bw_volume}|{physical_page}}}}}
}}}}

== File license ==
{{{{Baha'i World excerpt}}}}
"""
    elif is_ab_issue and ab_vol is not None and ab_issue is not None and physical_page is not None:
        content = f"""{access_block}== File info ==
{{{{cs
| caption = {caption}
| source = {{{{ab|{ab_vol}|{ab_issue}|{physical_page}}}}}
}}}}

== File license ==
{{{{Abn-copyright}}}}
"""
    else:
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

pdf_filename = st.text_input("PDF Filename", placeholder="e.g., The_American_Bahá’í_Vol2_No1.pdf")
page_ranges = st.text_input("Page Ranges", placeholder="e.g., 527-546, 12, 15-20")
skip_crop_ranges = st.text_input("Full Page Document Ranges (Skip Cropping)", placeholder="e.g., 5, 10-12")
access_control = st.text_input("Access Control (Optional)", placeholder="e.g., <accesscontrol>Access:DayVeryGreatThings</accesscontrol>")

if st.button("🚀 Process Images", type="primary"):
    if not pdf_filename:
        st.warning("Please provide a PDF filename.")
        st.stop()
        
    if not page_ranges and not skip_crop_ranges:
        st.warning("Please provide at least one page range.")
        st.stop()
        
    local_pdf_path = find_local_pdf(pdf_filename, input_folder)
    
    if not local_pdf_path:
        st.error(f"❌ Could not find {pdf_filename} in {input_folder}")
        st.stop()
        
    standard_pages = parse_range_string(page_ranges) if page_ranges else []
    skip_crop_pages = parse_range_string(skip_crop_ranges) if skip_crop_ranges else []
    pages_to_process = sorted(list(set(standard_pages + skip_crop_pages)))
    
    if not pages_to_process:
        st.warning("No valid pages found in the provided ranges.")
        st.stop()
        
    pdf_dir = os.path.dirname(local_pdf_path)
    clean_pdf_name = re.sub(r'\.pdf$', '', pdf_filename, flags=re.IGNORECASE)
    output_dir = os.path.join(pdf_dir, "images")
    
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    st.info(f"📂 Output directory: {output_dir}")
        
    progress_bar = st.progress(0)
    status_text = st.empty()
    log_container = st.container(border=True)
    
    # --- Detect publication type ---
    bw_match = re.search(r'BW_Volume(\d+)\.pdf', pdf_filename, re.IGNORECASE)
    is_bw_volume = bool(bw_match)
    bw_volume_num = int(bw_match.group(1)) if is_bw_volume else None

    # Handles The_American_Bahá’í_Vol2_No1.pdf (allows optional apostrophe for safety)
    ab_match = re.search(r'The_American_Bahá[’\']í_Vol(\d+)_No(\d+)', pdf_filename, re.IGNORECASE)
    is_ab_issue = bool(ab_match)
    ab_vol_num = int(ab_match.group(1)) if is_ab_issue else None
    ab_issue_num = int(ab_match.group(2)) if is_ab_issue else None

    # --- Fetch Maps ---
    bw_offset_map = {}
    ab_dp_map = {}
    ab_off_map = {}
    
    if is_bw_volume:
        log_container.info(f"📚 Detected Bahá'í World Volume {bw_volume_num}. Fetching offset map...")
        bw_offset_map = fetch_bw_offset_map("Module:BahaiWorld")
    elif is_ab_issue:
        log_container.info(f"📰 Detected American Bahá'í Vol {ab_vol_num} No {ab_issue_num}. Fetching offset maps...")
        ab_dp_map, ab_off_map = fetch_ab_maps("Module:AmericanBahai")

    for idx, page_num in enumerate(pages_to_process):
        status_text.markdown(f"**Processing Page {page_num} ({idx+1}/{len(pages_to_process)})...**")

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

        is_skip_crop = page_num in skip_crop_pages

        if is_skip_crop:
            log_container.write("🧠 Requesting captions and filenames from Gemini (Document Mode)...")
            gemini_data_list = extract_image_caption_and_filename(pil_img, default_name=f"page_{page_num}_image.png", is_full_page_doc=True)
        else:
            log_container.write("🧠 Requesting captions and filenames from Gemini...")
            gemini_data_list = extract_image_caption_and_filename(pil_img, default_name=f"page_{page_num}_image.png")

        if not gemini_data_list:
            log_container.warning(f"⚠️ No images detected by Gemini on page {page_num}. Skipping.")
            continue
            
        if is_skip_crop:
            log_container.write("⏭️ Skipping auto-crop (full page document mode).")
            cropped_cv2_images = [cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)]
        else:
            log_container.write(f"✂️ Auto-cropping {len(gemini_data_list)} image(s) from page...")
            cropped_cv2_images = crop_illustrations(pil_img, expected_count=len(gemini_data_list))
        
        for i, img_data in enumerate(gemini_data_list):
            caption = img_data.get("caption", "")
            proposed_filename = img_data.get("filename", f"page_{page_num}_image_{i+1}.png")
            
            if "[CAPTION TOO LONG - INSERT MANUALLY]" in caption:
                log_container.warning(f"⚠️ Image {i+1} on page {page_num} requires manual caption entry due to length.")
            
            final_img_path = os.path.join(output_dir, proposed_filename)
            counter = 1
            while os.path.exists(final_img_path):
                name, ext = os.path.splitext(proposed_filename)
                final_img_path = os.path.join(output_dir, f"{name}_{counter}{ext}")
                counter += 1
                
            final_filename = os.path.basename(final_img_path)
            final_txt_path = os.path.join(output_dir, final_filename.replace(".png", ".txt"))
            
            if i < len(cropped_cv2_images):
                cv2.imwrite(final_img_path, cropped_cv2_images[i])
            else:
                log_container.warning(f"⚠️ Could not auto-crop image {i+1}. Saving uncropped page.")
                pil_img.save(final_img_path) 
                
            log_container.write(f"📝 Generating MediaWiki text file for {final_filename}...")
            
            # --- Calculate Physical Page ---
            physical_page = None
            if is_bw_volume:
                physical_page = page_num - bw_offset_map.get(bw_volume_num, 0)
            elif is_ab_issue:
                physical_page = calculate_ab_physical_page(page_num, ab_vol_num, ab_issue_num, ab_dp_map, ab_off_map)
            
            create_wiki_text_file(
                final_txt_path, 
                caption, 
                clean_pdf_name, 
                access_control,
                is_bw_volume=is_bw_volume,
                bw_volume=bw_volume_num,
                is_ab_issue=is_ab_issue,
                ab_vol=ab_vol_num,
                ab_issue=ab_issue_num,
                physical_page=physical_page
            )
            
            log_container.success(f"✅ Finished page {page_num}, image {i+1} -> Saved as `{final_filename}`")
            
        progress_bar.progress((idx + 1) / len(pages_to_process))
        log_container.success(f"✅ Finished page {page_num}")
        
    status_text.success("🎉 All images extracted and processed successfully!")
