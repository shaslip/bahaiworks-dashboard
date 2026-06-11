import streamlit as st
import os
import sys
import re
import json
import base64
import requests
import unicodedata
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from streamlit_drawable_canvas import st_canvas

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Imports ---
from src.face_detection import detect_faces
from src.gemini_processor import map_faces_to_caption
from src.mediawiki_uploader import (
    check_category_exists_on_media, 
    check_categories_batch, 
    get_category_files, 
    fetch_wikitext, 
    get_image_url, 
    upload_to_mediawiki
)

st.set_page_config(page_title="Image Annotation", page_icon="🏷️", layout="wide")

# ==============================================================================
# HELPER FUNCTIONS & COLOR MAPPING
# ==============================================================================

NAMED_COLORS = [
    ("#FF0000", "Red"),
    ("#00FF00", "Green"),
    ("#0000FF", "Blue"),
    ("#FFFF00", "Yellow"),
    ("#FF00FF", "Magenta"),
    ("#00FFFF", "Cyan"),
    ("#FFA500", "Orange"),
    ("#800080", "Purple"),
    ("#00FA9A", "Spring Green"),
    ("#FF1493", "Deep Pink")
]

MEDIA_API_URL = 'https://bahai.media/api.php'

def normalize_name(name):
    """Removes accents and transliteration marks (like ‘ and ’) from names."""
    if not name: return name
    # Remove standard accents (á -> a, í -> i, etc.)
    n = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('utf-8')
    # Remove specific apostrophes/quotes
    n = re.sub(r"['‘’`]", "", n)
    return n.strip()

def get_color_name(hex_code):
    """Translates a hex code to a human-readable color name for the UI."""
    hex_code = hex_code.upper()
    for h, name in NAMED_COLORS:
        if h == hex_code:
            return name
    return "Custom Color"

def get_caption_from_text(content):
    """Extracts caption from wikitext content."""
    if not content: return ""
    match = re.search(r'\|\s*caption\s*=\s*(.*?)\n\|', content, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

def draw_numbered_boxes(pil_img, faces):
    img_copy = pil_img.copy()
    draw = ImageDraw.Draw(img_copy)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except IOError:
        font = ImageFont.load_default()

    for face in faces:
        x, y, w, h = face['box']
        box_id = face['id']
        draw.rectangle([x, y, x+w, y+h], outline="red", width=5)
        draw.rectangle([x, max(0, y-40), x+40, y], fill="red")
        draw.text((x+5, max(0, y-40)), str(box_id), fill="white", font=font)
        
    return img_copy

def pil_to_base64(pil_img):
    buffered = BytesIO()
    pil_img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_str}"

def generate_fabric_json(faces, pil_img, canvas_w, canvas_h):
    orig_w, orig_h = pil_img.size
    scale_x = canvas_w / orig_w
    scale_y = canvas_h / orig_h
    
    objects = []
    
    for i, face in enumerate(faces):
        x, y, w, h = face['box']
        color_hex = NAMED_COLORS[i % len(NAMED_COLORS)][0]
        objects.append({
            "type": "rect",
            "left": x * scale_x,
            "top": y * scale_y,
            "width": w * scale_x,
            "height": h * scale_y,
            "fill": "rgba(0,0,0,0)",
            "stroke": color_hex,
            "strokeWidth": 3,
            "selectable": True,
            "hasControls": True
        })
        
    resized_img = pil_img.resize((canvas_w, canvas_h))
    bg_b64 = pil_to_base64(resized_img)
        
    return {
        "version": "4.4.0",
        "objects": objects,
        "backgroundImage": {
            "type": "image",
            "src": bg_b64,
            "originX": "left",
            "originY": "top",
            "left": 0,
            "top": 0,
            "width": canvas_w,
            "height": canvas_h
        }
    }

def append_annotations_to_txt(txt_path, annotations_wikitext):
    with open(txt_path, 'a', encoding='utf-8') as f:
        f.write(f"\n\n{annotations_wikitext}")

def load_wiki_batch(files_list, start_idx, batch_size=15):
    """Fetches text and URLs for a batch of wiki files."""
    batch = files_list[start_idx:start_idx + batch_size]
    results = []
    for f in batch:
        text, _ = fetch_wikitext(f, api_url=MEDIA_API_URL)
        url = get_image_url(f, api_url=MEDIA_API_URL)
        results.append({
            "type": "wiki",
            "filename": f,
            "image_url": url,
            "text_content": text or ""
        })
    return results

# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================

if "pending_queue" not in st.session_state:
    st.session_state.pending_queue = [] # List of dicts
if "anno_queue" not in st.session_state:
    st.session_state.anno_queue = [] # List of dicts
if "current_idx" not in st.session_state:
    st.session_state.current_idx = 0
if "current_ai_data" not in st.session_state:
    st.session_state.current_ai_data = None
    
# Wiki pagination state
if "wiki_all_files" not in st.session_state:
    st.session_state.wiki_all_files = []
if "wiki_offset" not in st.session_state:
    st.session_state.wiki_offset = 0

# ==============================================================================
# UI & MAIN LOGIC
# ==============================================================================

st.title("🏷️ AI-Assisted Image Annotation")

# --- STAGE 0: SELECT FILES (THE QUEUE REVIEW) ---
if not st.session_state.anno_queue:
    
    st.sidebar.header("Configuration")
    tab_local, tab_wiki = st.sidebar.tabs(["📁 Local Files", "🌐 Wiki Files"])
    
    with tab_local:
        folder_path = st.text_input("Images Folder Path", value="/home/sarah/Desktop/Projects/Bahai.works/English/images/")
        if st.button("Scan Folder"):
            if os.path.exists(folder_path):
                valid_files = []
                for f in sorted(os.listdir(folder_path)):
                    if f.lower().endswith('.png'):
                        txt_file = f.replace('.png', '.txt')
                        txt_path = os.path.join(folder_path, txt_file)
                        if os.path.exists(txt_path):
                            with open(txt_path, 'r', encoding='utf-8') as tf:
                                txt_content = tf.read()
                            valid_files.append({
                                "type": "local",
                                "filename": f,
                                "image_path": os.path.join(folder_path, f),
                                "text_path": txt_path,
                                "text_content": txt_content
                            })
                st.session_state.pending_queue = valid_files
                st.session_state.wiki_all_files = [] # Clear wiki state
                st.rerun()
            else:
                st.error("Invalid folder path.")
                
    with tab_wiki:
        wiki_cat = st.text_input("Category (e.g. Category:Baha'i News No 548)")
        wiki_file = st.text_input("Specific File (e.g. File:Albert_Windust_1897.png)")
        
        if st.button("Fetch Wiki Files"):
            with st.spinner("Fetching from bahai.media..."):
                if wiki_file:
                    st.session_state.wiki_all_files = [wiki_file]
                elif wiki_cat:
                    st.session_state.wiki_all_files = get_category_files(wiki_cat, api_url=MEDIA_API_URL)
                else:
                    st.warning("Please enter a category or a file.")
                    st.stop()
                
                st.session_state.wiki_offset = 0
                st.session_state.pending_queue = load_wiki_batch(st.session_state.wiki_all_files, 0)
                st.rerun()
                
        # Pagination controls for Wiki
        if st.session_state.wiki_all_files and st.session_state.wiki_offset + 15 < len(st.session_state.wiki_all_files):
            st.divider()
            st.write(f"Showing {st.session_state.wiki_offset} to {st.session_state.wiki_offset + 15} of {len(st.session_state.wiki_all_files)} files.")
            if st.button("Load Next 15 Files"):
                with st.spinner("Fetching next batch..."):
                    st.session_state.wiki_offset += 15
                    st.session_state.pending_queue = load_wiki_batch(st.session_state.wiki_all_files, st.session_state.wiki_offset)
                    st.rerun()

    if st.session_state.pending_queue:
        # Wrap in a form so checking boxes doesn't trigger a slow rerun every time
        with st.form(key="queue_review_form"):
            st.write("### Review Queue")
            st.write("Review the images and their text. **Check the box** for any images you want to annotate.")
            st.divider()
            
            for item in st.session_state.pending_queue:
                # Adjust column ratios to let the text area fill space and push the checkbox right
                col1, col2, col3 = st.columns([2, 6, 0.5])
                
                display_img = item.get("image_path") if item["type"] == "local" else item.get("image_url")
                
                with col1:
                    if display_img:
                        st.image(display_img, width='stretch')
                    else:
                        st.warning("Image not found")
                with col2:
                    st.text_area("Text Content", item["text_content"], height=250, key=f"txt_{item['filename']}", disabled=True)
                with col3:
                    st.checkbox("Select", value=False, key=f"check_{item['filename']}", label_visibility="collapsed")
                st.divider()
                
            # Submit button for the form
            submit_queue = st.form_submit_button("🚀 Process Selected Images", type="primary")
            
            if submit_queue:
                # Gather only the files where the checkbox was True
                selected_items = [item for item in st.session_state.pending_queue if st.session_state.get(f"check_{item['filename']}", False)]
                
                if selected_items:
                    st.session_state.anno_queue = selected_items
                    st.session_state.current_idx = 0
                    st.session_state.current_ai_data = None
                    st.session_state.pending_queue = [] 
                    st.rerun()
                else:
                    st.warning("Please select at least one image to process.")

# --- STAGE 1: REVIEW & EDIT ---
if st.session_state.anno_queue:
    
    if st.session_state.current_idx >= len(st.session_state.anno_queue):
        st.success("🎉 All selected images have been annotated!")
        if st.button("Start Over"):
            st.session_state.anno_queue = []
            st.session_state.current_idx = 0
            st.session_state.current_ai_data = None
            st.rerun()
        st.stop()

    current_item = st.session_state.anno_queue[st.session_state.current_idx]
    filename = current_item["filename"]
    
    st.markdown(f"### Image {st.session_state.current_idx + 1} of {len(st.session_state.anno_queue)}: `{filename}`")
    
    # Load Image (Local or Wiki)
    if current_item["type"] == "local":
        pil_img = Image.open(current_item["image_path"])
    else:
        # Fetch image from URL into memory
        response = requests.get(current_item["image_url"])
        pil_img = Image.open(BytesIO(response.content))
        
    orig_w, orig_h = pil_img.size
    
    canvas_display_w = 700
    canvas_display_h = int(orig_h * (canvas_display_w / orig_w))
    
    # Calculate scale factors for cropping and saving
    scale_x = orig_w / canvas_display_w
    scale_y = orig_h / canvas_display_h

    # 1. AI Processing
    if st.session_state.current_ai_data is None:
        with st.spinner("🤖 AI is detecting faces and reading the caption..."):
            caption = get_caption_from_text(current_item["text_content"])
            
            faces = detect_faces(pil_img)
            
            mapped_names = []
            if faces and caption:
                numbered_img = draw_numbered_boxes(pil_img, faces)
                mapped_names = map_faces_to_caption(numbered_img, caption)
            elif not faces and caption:
                mapped_names = map_faces_to_caption(pil_img, caption)
                
            # Normalize names extracted by Gemini
            for item in mapped_names:
                item["name"] = normalize_name(item["name"])
                
            # Verify all names in ONE single API request
            names_to_check = [item["name"] for item in mapped_names]
            category_status = check_categories_batch(names_to_check)
            
            for item in mapped_names:
                item["exists"] = category_status.get(item["name"], False)
                
            canvas_json = generate_fabric_json(faces, pil_img, canvas_display_w, canvas_display_h)
            
            st.session_state.current_ai_data = {
                "caption": caption,
                "faces": faces,
                "mapped_names": mapped_names,
                "canvas_json": canvas_json,
                "manual_names": []
            }
            st.rerun()

    # 2. Render the Review UI
    ai_data = st.session_state.current_ai_data

    st.info(f"**Caption:** {ai_data['caption']}")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.write("✏️ **Draw, Move, or Delete Boxes** (Select a box and press Delete/Backspace to remove)")
        
        canvas_result = st_canvas(
            fill_color="rgba(0, 0, 0, 0)",
            stroke_width=3,
            stroke_color="#FF0000", # Default color if user draws a new box
            background_image=None, 
            update_streamlit=True,
            height=canvas_display_h,
            width=canvas_display_w,
            drawing_mode="rect",
            initial_drawing=ai_data["canvas_json"],
            key=f"canvas_{st.session_state.current_idx}",
        )

    with col2:
        st.write("📋 **Map Names to Boxes**")
        
        current_boxes = []
        if canvas_result.json_data is not None:
            current_boxes = canvas_result.json_data["objects"]
            
        # Dynamically generate dropdown options based on the colors currently in the canvas
        box_options = ["None"]
        for i, box in enumerate(current_boxes):
            stroke_color = box.get("stroke", "#FF0000").upper()
            color_name = get_color_name(stroke_color)
            box_options.append(f"Box {i+1} ({color_name})")
        
        final_mappings = {}
        all_names_to_map = ai_data["mapped_names"] + ai_data["manual_names"]
        
        for i, item in enumerate(all_names_to_map):
            orig_name = item["name"]
            ai_box_id = item.get("box_id")
            is_verified = item.get("exists", False)
            
            default_idx = 0
            if ai_box_id is not None and 1 <= ai_box_id <= len(current_boxes):
                default_idx = ai_box_id
            
            col_img, col_name, col_box = st.columns([1, 2, 1.5])
            
            # 1. Render the Selectbox FIRST so we can use its live value
            with col_box:
                selected_box = st.selectbox(
                    "Assign Box:",
                    options=box_options,
                    index=default_idx,
                    key=f"map_box_{i}"
                )
            
            # 2. Render the Thumbnail based on what the user ACTUALLY selected
            with col_img:
                if selected_box != "None":
                    current_box_idx = int(re.search(r'Box (\d+)', selected_box).group(1)) - 1
                    box = current_boxes[current_box_idx]
                    
                    left = int(box["left"] * scale_x)
                    top = int(box["top"] * scale_y)
                    w = int(box["width"] * box.get("scaleX", 1) * scale_x)
                    h = int(box["height"] * box.get("scaleY", 1) * scale_y)
                    
                    face_crop = pil_img.crop((left, top, left + w, top + h))
                    st.image(face_crop, width=80)
                    
                    stroke_color = box.get("stroke", "#FF0000").upper()
                    color_name = get_color_name(stroke_color)
                    st.caption(f"Box {current_box_idx + 1} ({color_name})")
                else:
                    st.markdown("<div style='height:80px; width:80px; background-color:#333; display:flex; align-items:center; justify-content:center; border-radius:5px; color:#fff; font-size:12px;'>No Face</div>", unsafe_allow_html=True)

            # 3. Render the Name editor
            with col_name:
                if is_verified:
                    st.markdown(f"<div style='padding-top:20px;'>✅ <b>{orig_name}</b></div>", unsafe_allow_html=True)
                    final_name = orig_name
                else:
                    final_name = st.text_input(f"⚠️ Category not found. Edit:", value=orig_name, key=f"edit_name_{i}")
            
            # 4. Save the mapping
            if selected_box != "None":
                current_box_idx = int(re.search(r'Box (\d+)', selected_box).group(1)) - 1
                final_mappings[final_name] = current_box_idx
                
            st.markdown("<hr style='margin: 10px 0;'>", unsafe_allow_html=True)

        submit_btn = st.button("💾 Save Annotations & Next", type="primary")

        st.write("➕ **Missed a name?**")
        new_name = st.text_input("Enter name:")
        if st.button("Add Name"):
            norm_name = normalize_name(new_name)
            if norm_name and norm_name not in [n["name"] for n in ai_data["mapped_names"]] and norm_name not in [n["name"] for n in ai_data["manual_names"]]:
                exists = check_category_exists_on_media(norm_name)
                st.session_state.current_ai_data["manual_names"].append({"name": norm_name, "box_id": None, "exists": exists})
                st.rerun()

        if st.button("⏭️ Skip Image"):
            st.session_state.current_idx += 1
            st.session_state.current_ai_data = None
            st.rerun()

    # 3. Process Save Action
    if submit_btn:
        if not final_mappings:
            st.warning("No mappings selected. Skipping save.")
            st.session_state.current_idx += 1
            st.session_state.current_ai_data = None
            st.rerun()
            
        wikitext_blocks = []
        
        for i, (name, box_idx) in enumerate(final_mappings.items()):
            box = current_boxes[box_idx]
            
            true_x = int(box["left"] * scale_x)
            true_y = int(box["top"] * scale_y)
            true_w = int(box["width"] * box.get("scaleX", 1) * scale_x)
            true_h = int(box["height"] * box.get("scaleY", 1) * scale_y)
            
            template = f"{{{{ImageNote|id={i+1}|x={true_x}|y={true_y}|w={true_w}|h={true_h}|dimx={orig_w}|dimy={orig_h}|style=2}}}}\n"
            template += f"{{{{ia|{name}}}}}\n"
            template += f"{{{{ImageNoteEnd|id={i+1}}}}}"
            wikitext_blocks.append(template)
            
        final_wikitext = "\n".join(wikitext_blocks)
        
        if current_item["type"] == "local":
            append_annotations_to_txt(current_item["text_path"], final_wikitext)
            st.success("Saved to local file!")
        else:
            with st.spinner("Uploading to bahai.media..."):
                new_content = current_item["text_content"] + "\n\n" + final_wikitext
                with requests.Session() as session:
                    upload_to_mediawiki(
                        title=current_item["filename"], 
                        content=new_content, 
                        summary="Added image annotations via AI tool", 
                        session=session, 
                        api_url=MEDIA_API_URL
                    )
            st.success("Saved to wiki!")
            
        st.session_state.current_idx += 1
        st.session_state.current_ai_data = None
        st.rerun()
