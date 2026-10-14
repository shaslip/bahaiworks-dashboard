import streamlit as st
import os
import sys
import re
import cv2
import numpy as np
import io
from PIL import Image
import requests
import fitz

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.gemini_processor import parse_range_string, extract_image_caption_and_filename
from src.calibration import calculate_start_offset

st.set_page_config(page_title="Book Image Extractor", page_icon="🖼️", layout="wide")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def crop_collage_images(pil_img, expected_count=0):
    """Crops sub-images from a white-bordered collage (auto-detects count if expected_count == 0)."""
    img = np.array(pil_img)
    img = img[:, :, ::-1].copy()

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # White borders are near 255; threshold at 245 so dark/medium regions inside photos stay connected
    _, thresh = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY_INV)

    # Light morphology close to bridge tiny white highlights inside a single photo without crossing the white gutter
    kernel = np.ones((3, 3), np.uint8)
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []

    total_area = img.shape[0] * img.shape[1]
    min_area = total_area * 0.02  # Ignore tiny noise specks (< 2% of total image)

    valid_contours = [c for c in contours if cv2.contourArea(c) >= min_area]
    sorted_by_area = sorted(valid_contours, key=cv2.contourArea, reverse=True)

    if expected_count > 0:
        top_contours = sorted_by_area[:expected_count]
    else:
        top_contours = sorted_by_area

    # Sort top-to-bottom, then left-to-right
    top_contours = sorted(top_contours, key=lambda c: (cv2.boundingRect(c)[1] // 100, cv2.boundingRect(c)[0]))

    cropped_images = []
    for c in top_contours:
        x, y, w, h = cv2.boundingRect(c)
        cropped_images.append(img[y:y+h, x:x+w])

    return cropped_images

@st.cache_data(ttl=600)
def fetch_category_images(category_name, thumb_width=400):
    """Fetches image metadata, thumbnail URLs, full URLs, and wikitext from a bahai.media category."""
    url = "https://bahai.media/api.php"
    if not category_name.lower().startswith("category:"):
        category_name = f"Category:{category_name}"

    results = []
    gcmcontinue = None

    while True:
        params = {
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": category_name,
            "gcmtype": "file",
            "gcmlimit": "50",
            "prop": "imageinfo|revisions",
            "iiprop": "url",
            "iiurlwidth": thumb_width,
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
        }
        if gcmcontinue:
            params["gcmcontinue"] = gcmcontinue

        try:
            resp = requests.get(url, params=params, timeout=30)
            data = resp.json()
        except Exception as e:
            print(f"Error fetching category {category_name}: {e}")
            break

        pages = data.get("query", {}).get("pages", {})
        for page_id, page_data in pages.items():
            if int(page_id) < 0:
                continue
            title = page_data.get("title", "")
            filename = re.sub(r"^File:", "", title, flags=re.IGNORECASE).replace(" ", "_")

            iinfo = page_data.get("imageinfo", [{}])[0]
            full_url = iinfo.get("url", "")
            thumb_url = iinfo.get("thumburl", full_url)

            wikitext = ""
            revs = page_data.get("revisions", [])
            if revs:
                wikitext = revs[0].get("slots", {}).get("main", {}).get("*", "")

            if full_url:
                results.append({
                    "title": title,
                    "filename": filename,
                    "url": full_url,
                    "thumb_url": thumb_url,
                    "wikitext": wikitext,
                })

        if "continue" in data and "gcmcontinue" in data["continue"]:
            gcmcontinue = data["continue"]["gcmcontinue"]
        else:
            break

    return sorted(results, key=lambda x: x["filename"].lower())

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
    
    # Use Otsu's method to automatically find the threshold for the beige background
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    
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
        
        # Apply Otsu's to the tight crop as well
        _, thresh_cropped = cv2.threshold(gray_cropped, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
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
                          is_ab_issue=False, ab_vol=None, ab_issue=None, physical_page=None,
                          pdf_page=None, misc_offset=None, category_override=None, file_license=None):
    clean_title = re.sub(r'\.pdf$', '', book_title, flags=re.IGNORECASE).replace('_', ' ')
    access_block = f"{access_control.strip()}\n" if access_control.strip() else ""
    
    if is_bw_volume and bw_volume is not None and physical_page is not None:
        content = f"""{access_block}== File info ==
{{{{cs
| caption = {caption}
| source = {{{{bws|{bw_volume}|{physical_page}}}}}
}}}}
"""
        default_license = "{{Baha'i World excerpt}}"
    elif is_ab_issue and ab_vol is not None and ab_issue is not None and physical_page is not None:
        content = f"""{access_block}== File info ==
{{{{cs
| caption = {caption}
| source = {{{{ab|{ab_vol}|{ab_issue}|{physical_page}}}}}
}}}}
"""
        default_license = "{{Abn-copyright}}"
    else:
        cat_param = ""
        if category_override and category_override.strip():
            clean_cat = re.sub(r'^Category:', '', category_override.strip(), flags=re.IGNORECASE).strip()
            cat_param = f"|category={clean_cat}"
            
        if misc_offset is not None and pdf_page is not None:
            calc_phys_page = pdf_page - misc_offset
            source_str = f"{{{{misc|{clean_title}|{calc_phys_page}|pdfpage={pdf_page}{cat_param}}}}}"
        elif pdf_page is not None:
            source_str = f"{{{{misc|{clean_title}|pdfpage={pdf_page}{cat_param}}}}}"
        else:
            source_str = clean_title
            
        content = f"""{access_block}== File info ==
{{{{cs
| caption = {caption}
| source = {source_str}
}}}}
"""
        default_license = ""

    final_license = file_license.strip() if file_license and file_license.strip() else default_license
    
    if final_license:
        content += f"\n== File license ==\n{final_license}\n"

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(content)

# ==============================================================================
# UI & MAIN LOGIC
# ==============================================================================

st.title("🖼️ Image Extractor")

tab1, tab2, tab3 = st.tabs([
    "📚 Book Image Extractor", 
    "✂️ Local Collage Cropper", 
    "🌐 Wiki Category Cropper"
])

with tab1:
    st.sidebar.header("Configuration")
    
    input_folder = st.sidebar.text_input("Local PDF Root Folder", value="/home/sarah/Desktop/Projects/Bahai.works/English/")

    pdf_filename = st.text_input("PDF Filename", placeholder="e.g., The_American_Bahá’í_Vol2_No1.pdf")
    page_ranges = st.text_input("Page Ranges", placeholder="e.g., 1-10, 12, 14-15")
    skip_crop_ranges = st.text_input("Full Page Document Ranges (Skip Cropping)", placeholder="e.g., 11, 16")
    access_control = st.text_input("Access Control (Optional)", placeholder="e.g., <accesscontrol>Access:DayVeryGreatThings</accesscontrol>")

    st.divider()
    st.subheader("Page and Category Overrides")

    category_override = st.text_input("Media Category (Optional)", placeholder="e.g., Category:Visiting_Bahá’í_Holy_Places")
    file_license = st.text_input("File license (Optional)", placeholder="e.g., {{PD-US}}")

    offset_mode = st.radio(
        "Select Offset Mode",
        ["Auto-detect Offset", "Manual Offset", "Fall back to electronic page"],
        help="Determines how physical page numbers are calculated for generic books."
    )

    misc_offset_input = None
    if offset_mode == "Manual Offset":
        misc_offset_input = st.text_input("Enter Manual Offset", placeholder="e.g., 14")

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

        # --- Fetch Maps & Process Generic Offsets ---
        bw_offset_map = {}
        ab_dp_map = {}
        ab_off_map = {}
        misc_offset = None
        
        if is_bw_volume:
            log_container.info(f"📚 Detected Bahá'í World Volume {bw_volume_num}. Fetching offset map...")
            bw_offset_map = fetch_bw_offset_map("Module:BahaiWorld")
        elif is_ab_issue:
            log_container.info(f"📰 Detected American Bahá'í Vol {ab_vol_num} No {ab_issue_num}. Fetching offset maps...")
            ab_dp_map, ab_off_map = fetch_ab_maps("Module:AmericanBahai")
        else:
            # Generic book offset handling
            if offset_mode == "Manual Offset":
                if misc_offset_input and misc_offset_input.strip():
                    try:
                        misc_offset = int(misc_offset_input.strip())
                        log_container.info(f"🔢 Using manual offset: {misc_offset}")
                    except ValueError:
                        st.warning("Offset must be a valid integer.")
                        st.stop()
            elif offset_mode == "Auto-detect Offset":
                log_container.info("🔍 Auto-detecting page offset...")
                try:
                    with fitz.open(local_pdf_path) as pdf:
                        total_pages = len(pdf)
                    detected_offset, _ = calculate_start_offset(local_pdf_path, total_pages)
                    if detected_offset is not None:
                        misc_offset = detected_offset
                        log_container.success(f"✅ Auto-detected offset: {misc_offset}")
                    else:
                        log_container.warning("⚠️ Could not auto-detect offset. Falling back to electronic page.")
                        misc_offset = None
                except Exception as e:
                    log_container.error(f"❌ Error during auto-detection: {e}")
                    misc_offset = None

        for idx, page_num in enumerate(pages_to_process):
            status_text.markdown(f"**Processing Page {page_num} ({idx+1}/{len(pages_to_process)})...**")

            log_container.write(f"📄 Extracting raw scan from page {page_num}...")
            try:
                with fitz.open(local_pdf_path) as doc:
                    # fitz uses 0-based indexing for pages, so we subtract 1
                    page = doc[page_num - 1]
                    image_list = page.get_images(full=True)
                    
                    if not image_list:
                        log_container.warning(f"⚠️ No raw images found on page {page_num}. Skipping.")
                        continue
                    
                    # Find the largest image on the page (the full scanned page)
                    largest_image = max(image_list, key=lambda img: img[2] * img[3])
                    xref = largest_image[0]
                    
                    # Extract the raw bytes of the original scan
                    extracted_image = doc.extract_image(xref)
                    image_bytes = extracted_image["image"]
                    
                    # Convert bytes to a PIL Image so Gemini and OpenCV can use it unmodified
                    pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
                    
            except Exception as e:
                log_container.error(f"❌ Error extracting raw image from page {page_num}: {e}")
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
                    physical_page=physical_page,
                    pdf_page=page_num,
                    misc_offset=misc_offset,
                    category_override=category_override,
                    file_license=file_license
                )
                
                log_container.success(f"✅ Finished page {page_num}, image {i+1} -> Saved as `{final_filename}`")
                
            progress_bar.progress((idx + 1) / len(pages_to_process))
            log_container.success(f"✅ Finished page {page_num}")
            
        status_text.success("🎉 All images extracted and processed successfully!")

with tab2:
    st.subheader("✂️ Crop Local Collage Images")
    col_dir, col_count = st.columns([3, 1])
    with col_dir:
        collage_dir = st.text_input(
            "Input Directory",
            value="/home/sarah/Desktop/Projects/Tools/Bahaimedia/output/",
        )
    with col_count:
        expected_subimages = st.number_input(
            "Expected Sub-images (0 = Auto-detect)",
            min_value=0,
            max_value=12,
            value=0,
            step=1,
        )

    delete_originals = st.checkbox("Move original uncropped collage files into an `originals/` subfolder after cropping", value=True)

    if os.path.isdir(collage_dir):
        valid_exts = (".jpg", ".jpeg", ".png", ".webp")
        image_files = sorted([
            f for f in os.listdir(collage_dir)
            if os.path.isfile(os.path.join(collage_dir, f)) and f.lower().endswith(valid_exts)
        ])

        if not image_files:
            st.info(f"No image files found in `{collage_dir}`.")
        else:
            st.write(f"Found **{len(image_files)}** image(s) in `{collage_dir}`:")
            selected_files = []
            for fname in image_files:
                if st.checkbox(fname, value=True, key=f"crop_sel_{fname}"):
                    selected_files.append(fname)

            if st.button("✂️ Crop Selected Collages", type="primary"):
                if not selected_files:
                    st.warning("Please select at least one image to crop.")
                else:
                    crop_log = st.container(border=True)
                    originals_dir = os.path.join(collage_dir, "originals")
                    if delete_originals:
                        os.makedirs(originals_dir, exist_ok=True)

                    for fname in selected_files:
                        img_path = os.path.join(collage_dir, fname)
                        base_name, ext = os.path.splitext(fname)
                        txt_path = os.path.join(collage_dir, f"{base_name}.txt")

                        txt_content = None
                        if os.path.exists(txt_path):
                            with open(txt_path, "r", encoding="utf-8") as f:
                                txt_content = f.read()

                        pil_img = Image.open(img_path).convert("RGB")
                        crops = crop_collage_images(pil_img, expected_count=expected_subimages)

                        if len(crops) <= 1:
                            crop_log.warning(f"⚠️ Only detected {len(crops)} region in `{fname}` — skipping split.")
                            continue

                        for idx, crop_bgr in enumerate(crops, start=1):
                            out_img_name = f"{base_name}-{idx}{ext}"
                            out_img_path = os.path.join(collage_dir, out_img_name)
                            cv2.imwrite(out_img_path, crop_bgr)

                            if txt_content is not None:
                                out_txt_path = os.path.join(collage_dir, f"{base_name}-{idx}.txt")
                                with open(out_txt_path, "w", encoding="utf-8") as f:
                                    f.write(txt_content)

                        crop_log.success(f"✅ Split `{fname}` into **{len(crops)}** images (`{base_name}-1{ext}` .. `{base_name}-{len(crops)}{ext}`)")

                        if delete_originals:
                            os.replace(img_path, os.path.join(originals_dir, fname))
                            if os.path.exists(txt_path):
                                os.replace(txt_path, os.path.join(originals_dir, f"{base_name}.txt"))

with tab2:
    st.subheader("✂️ Crop Collage Images")

    source_mode = st.radio(
        "Image Source",
        ["Local Directory", "Bahai.media Category"],
        horizontal=True,
    )

    col_dir, col_count = st.columns([3, 1])
    with col_dir:
        collage_dir = st.text_input(
            "Local Directory (Input / Output)",
            value="/home/sarah/Desktop/Projects/Tools/Bahaimedia/output/",
        )
    with col_count:
        expected_subimages = st.number_input(
            "Expected Sub-images (0 = Auto-detect)",
            min_value=0,
            max_value=12,
            value=0,
            step=1,
        )

    if source_mode == "Local Directory":
        delete_originals = st.checkbox("Move original uncropped collage files into an `originals/` subfolder after cropping", value=True)

        if os.path.isdir(collage_dir):
            valid_exts = (".jpg", ".jpeg", ".png", ".webp")
            image_files = sorted([
                f for f in os.listdir(collage_dir)
                if os.path.isfile(os.path.join(collage_dir, f)) and f.lower().endswith(valid_exts)
            ])

            if not image_files:
                st.info(f"No image files found in `{collage_dir}`.")
            else:
                st.write(f"Found **{len(image_files)}** image(s) in `{collage_dir}`:")
                selected_files = []
                for fname in image_files:
                    if st.checkbox(fname, value=True, key=f"crop_sel_{fname}"):
                        selected_files.append(fname)

                if st.button("✂️ Crop Selected Collages", type="primary"):
                    if not selected_files:
                        st.warning("Please select at least one image to crop.")
                    else:
                        crop_log = st.container(border=True)
                        originals_dir = os.path.join(collage_dir, "originals")
                        if delete_originals:
                            os.makedirs(originals_dir, exist_ok=True)

                        for fname in selected_files:
                            img_path = os.path.join(collage_dir, fname)
                            base_name, ext = os.path.splitext(fname)
                            txt_path = os.path.join(collage_dir, f"{base_name}.txt")

                            txt_content = None
                            if os.path.exists(txt_path):
                                with open(txt_path, "r", encoding="utf-8") as f:
                                    txt_content = f.read()

                            pil_img = Image.open(img_path).convert("RGB")
                            crops = crop_collage_images(pil_img, expected_count=expected_subimages)

                            if len(crops) <= 1:
                                crop_log.warning(f"⚠️ Only detected {len(crops)} region in `{fname}` — skipping split.")
                                continue

                            for idx, crop_bgr in enumerate(crops, start=1):
                                out_img_name = f"{base_name}-{idx}{ext}"
                                out_img_path = os.path.join(collage_dir, out_img_name)
                                cv2.imwrite(out_img_path, crop_bgr)

                                if txt_content is not None:
                                    out_txt_path = os.path.join(collage_dir, f"{base_name}-{idx}.txt")
                                    with open(out_txt_path, "w", encoding="utf-8") as f:
                                        f.write(txt_content)

                            crop_log.success(f"✅ Split `{fname}` into **{len(crops)}** images (`{base_name}-1{ext}` .. `{base_name}-{len(crops)}{ext}`)")

                            if delete_originals:
                                os.replace(img_path, os.path.join(originals_dir, fname))
                                if os.path.exists(txt_path):
                                    os.replace(txt_path, os.path.join(originals_dir, f"{base_name}.txt"))

    else:
        cat_col, btn_col = st.columns([3, 1])
        with cat_col:
            wiki_category = st.text_input(
                "Bahai.media Category",
                placeholder="e.g., Category:Images from The American Bahá'í",
            )
        with btn_col:
            st.write("")
            st.write("")
            fetch_clicked = st.button("🔍 Fetch Category Images")

        if fetch_clicked and wiki_category.strip():
            with st.spinner(f"Fetching images from `{wiki_category.strip()}`..."):
                st.session_state["cat_images"] = fetch_category_images(wiki_category.strip())

        cat_images = st.session_state.get("cat_images", [])
        if cat_images:
            st.write(f"Found **{len(cat_images)}** file(s). Select the collages you want to download and split:")

            cols_per_row = 3
            selected_cat_items = []
            for row_start in range(0, len(cat_images), cols_per_row):
                row_items = cat_images[row_start:row_start + cols_per_row]
                cols = st.columns(cols_per_row)
                for col, item in zip(cols, row_items):
                    with col:
                        with st.container(border=True):
                            st.image(item["thumb_url"], use_container_width=True)
                            if st.checkbox(item["filename"], value=False, key=f"cat_sel_{item['filename']}"):
                                selected_cat_items.append(item)

            if st.button("⬇️ Download & Crop Selected Collages", type="primary"):
                if not selected_cat_items:
                    st.warning("Please select at least one collage image.")
                else:
                    os.makedirs(collage_dir, exist_ok=True)
                    originals_dir = os.path.join(collage_dir, "originals")
                    os.makedirs(originals_dir, exist_ok=True)
                    crop_log = st.container(border=True)

                    for item in selected_cat_items:
                        fname = item["filename"]
                        base_name, ext = os.path.splitext(fname)
                        wikitext = item["wikitext"]

                        try:
                            resp = requests.get(item["url"], timeout=30)
                            resp.raise_for_status()
                            img_bytes = resp.content
                        except Exception as e:
                            crop_log.error(f"❌ Failed to download `{fname}`: {e}")
                            continue

                        # Save uncropped backup to originals/
                        with open(os.path.join(originals_dir, fname), "wb") as f:
                            f.write(img_bytes)
                        with open(os.path.join(originals_dir, f"{base_name}.txt"), "w", encoding="utf-8") as f:
                            f.write(wikitext)

                        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                        crops = crop_collage_images(pil_img, expected_count=expected_subimages)

                        if len(crops) <= 1:
                            crop_log.warning(f"⚠️ Only detected {len(crops)} region in `{fname}` — skipping split (backup saved in `originals/`).")
                            continue

                        # Crop 1 keeps the original filename; Crops 2..N use -1, -2, etc.
                        saved_names = []
                        for idx, crop_bgr in enumerate(crops):
                            if idx == 0:
                                out_base = base_name
                            else:
                                out_base = f"{base_name}-{idx}"

                            out_img_name = f"{out_base}{ext}"
                            out_img_path = os.path.join(collage_dir, out_img_name)
                            out_txt_path = os.path.join(collage_dir, f"{out_base}.txt")

                            cv2.imwrite(out_img_path, crop_bgr)
                            with open(out_txt_path, "w", encoding="utf-8") as f:
                                f.write(wikitext)

                            saved_names.append(out_img_name)

                        crop_log.success(f"✅ Split `{fname}` into **{len(crops)}** images: `{', '.join(saved_names)}`")

with tab3:
    st.subheader("🌐 Crop Wiki Category Collages")

    col_dir, col_count = st.columns([3, 1])
    with col_dir:
        wiki_collage_dir = st.text_input(
            "Output Directory",
            value="/home/sarah/Desktop/Projects/Tools/Bahaimedia/output/",
            key="wiki_collage_dir",
        )
    with col_count:
        wiki_expected_subimages = st.number_input(
            "Expected Sub-images (0 = Auto-detect)",
            min_value=0,
            max_value=12,
            value=0,
            step=1,
            key="wiki_expected_subimages",
        )

    cat_col, btn_col = st.columns([3, 1])
    with cat_col:
        wiki_category = st.text_input(
            "Bahai.media Category",
            placeholder="e.g., Category:Images from The American Bahá'í",
        )
    with btn_col:
        st.write("")
        st.write("")
        fetch_clicked = st.button("🔍 Fetch Category Images")

    if fetch_clicked and wiki_category.strip():
        with st.spinner(f"Fetching images from `{wiki_category.strip()}`..."):
            st.session_state["cat_images"] = fetch_category_images(wiki_category.strip())

    cat_images = st.session_state.get("cat_images", [])
    if cat_images:
        st.write(f"Found **{len(cat_images)}** file(s). Select the collages you want to download and split:")

        cols_per_row = 3
        selected_cat_items = []
        for row_start in range(0, len(cat_images), cols_per_row):
            row_items = cat_images[row_start:row_start + cols_per_row]
            cols = st.columns(cols_per_row)
            for col, item in zip(cols, row_items):
                with col:
                    with st.container(border=True):
                        st.image(item["thumb_url"], use_container_width=True)
                        if st.checkbox(item["filename"], value=False, key=f"cat_sel_{item['filename']}"):
                            selected_cat_items.append(item)

        if st.button("⬇️ Download & Crop Selected Collages", type="primary"):
            if not selected_cat_items:
                st.warning("Please select at least one collage image.")
            else:
                os.makedirs(wiki_collage_dir, exist_ok=True)
                originals_dir = os.path.join(wiki_collage_dir, "originals")
                os.makedirs(originals_dir, exist_ok=True)
                crop_log = st.container(border=True)

                for item in selected_cat_items:
                    fname = item["filename"]
                    base_name, ext = os.path.splitext(fname)
                    wikitext = item["wikitext"]

                    try:
                        resp = requests.get(item["url"], timeout=30)
                        resp.raise_for_status()
                        img_bytes = resp.content
                    except Exception as e:
                        crop_log.error(f"❌ Failed to download `{fname}`: {e}")
                        continue

                    # Save uncropped backup to originals/
                    with open(os.path.join(originals_dir, fname), "wb") as f:
                        f.write(img_bytes)
                    with open(os.path.join(originals_dir, f"{base_name}.txt"), "w", encoding="utf-8") as f:
                        f.write(wikitext)

                    pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    crops = crop_collage_images(pil_img, expected_count=wiki_expected_subimages)

                    if len(crops) <= 1:
                        crop_log.warning(f"⚠️ Only detected {len(crops)} region in `{fname}` — skipping split (backup saved in `originals/`).")
                        continue

                    # Crop 1 keeps the original filename; Crops 2..N use -1, -2, etc.
                    saved_names = []
                    for idx, crop_bgr in enumerate(crops):
                        if idx == 0:
                            out_base = base_name
                        else:
                            out_base = f"{base_name}-{idx}"

                        out_img_name = f"{out_base}{ext}"
                        out_img_path = os.path.join(wiki_collage_dir, out_img_name)
                        out_txt_path = os.path.join(wiki_collage_dir, f"{out_base}.txt")

                        cv2.imwrite(out_img_path, crop_bgr)
                        with open(out_txt_path, "w", encoding="utf-8") as f:
                            f.write(wikitext)

                        saved_names.append(out_img_name)

                    crop_log.success(f"✅ Split `{fname}` into **{len(crops)}** images: `{', '.join(saved_names)}`")
