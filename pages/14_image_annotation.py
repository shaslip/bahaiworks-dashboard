import streamlit as st
import os
import sys
import re
import json
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

st.set_page_config(page_title="Image Annotation", page_icon="🏷️", layout="wide")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def get_caption_from_txt(txt_path):
    """Extracts the caption string from the wikitext file."""
    if not os.path.exists(txt_path): return ""
    with open(txt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    # Look for the caption parameter in the {{cs}} template
    match = re.search(r'\|\s*caption\s*=\s*(.*?)\n\|', content, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

def draw_numbered_boxes(pil_img, faces):
    """Draws boxes and large ID numbers on a temporary image for Gemini to read."""
    img_copy = pil_img.copy()
    draw = ImageDraw.Draw(img_copy)
    
    # Try to load a default font, fallback to basic if not found
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except IOError:
        font = ImageFont.load_default()

    for face in faces:
        x, y, w, h = face['box']
        box_id = face['id']
        
        # Draw thick red box
        draw.rectangle([x, y, x+w, y+h], outline="red", width=5)
        
        # Draw background for text to make it readable
        text = str(box_id)
        # Using simple bounding box for text background
        draw.rectangle([x, max(0, y-40), x+40, y], fill="red")
        draw.text((x+5, max(0, y-40)), text, fill="white", font=font)
        
    return img_copy

def generate_fabric_json(faces):
    """Converts MTCNN boxes into the JSON format required by streamlit-drawable-canvas."""
    colors = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00", "#FF00FF", "#00FFFF"]
    objects = []
    
    for i, face in enumerate(faces):
        x, y, w, h = face['box']
        color = colors[i % len(colors)]
        objects.append({
            "type": "rect",
            "left": x,
            "top": y,
            "width": w,
            "height": h,
            "fill": "rgba(0,0,0,0)", # Transparent fill
            "stroke": color,
            "strokeWidth": 3,
            "selectable": True,
            "hasControls": True
        })
        
    return {"version": "4.4.0", "objects": objects}

def append_annotations_to_txt(txt_path, annotations_wikitext):
    """Appends the generated ImageNote templates to the bottom of the text file."""
    with open(txt_path, 'a', encoding='utf-8') as f:
        f.write(f"\n\n{annotations_wikitext}")

# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================

if "anno_queue" not in st.session_state:
    st.session_state.anno_queue = []
if "current_idx" not in st.session_state:
    st.session_state.current_idx = 0
if "current_ai_data" not in st.session_state:
    st.session_state.current_ai_data = None

# ==============================================================================
# UI & MAIN LOGIC
# ==============================================================================

st.title("🏷️ AI-Assisted Image Annotation")

st.sidebar.header("Configuration")
folder_path = st.sidebar.text_input("Images Folder Path", value="/home/sarah/Desktop/Projects/Bahai.works/English/images/")

# --- STAGE 0: SELECT FILES ---
if not st.session_state.anno_queue:
    st.write("Select images containing people to automatically detect faces and annotate them.")
    
    if st.button("Scan Folder"):
        if os.path.exists(folder_path):
            valid_files = []
            for f in sorted(os.listdir(folder_path)):
                if f.lower().endswith('.png'):
                    txt_file = f.replace('.png', '.txt')
                    if os.path.exists(os.path.join(folder_path, txt_file)):
                        valid_files.append(f)
            st.session_state.scanned_files = valid_files
        else:
            st.error("Invalid folder path.")

    if "scanned_files" in st.session_state and st.session_state.scanned_files:
        selected = st.multiselect("Select images to annotate:", st.session_state.scanned_files)
        
        if st.button("🚀 Start Annotation Process", type="primary") and selected:
            st.session_state.anno_queue = [os.path.join(folder_path, s) for s in selected]
            st.session_state.current_idx = 0
            st.session_state.current_ai_data = None
            st.rerun()

# --- STAGE 1: REVIEW & EDIT ---
if st.session_state.anno_queue:
    
    # Check if we are done
    if st.session_state.current_idx >= len(st.session_state.anno_queue):
        st.success("🎉 All selected images have been annotated!")
        if st.button("Start Over"):
            st.session_state.anno_queue = []
            st.session_state.current_idx = 0
            st.session_state.current_ai_data = None
            st.rerun()
        st.stop()

    current_img_path = st.session_state.anno_queue[st.session_state.current_idx]
    current_txt_path = current_img_path.replace('.png', '.txt')
    filename = os.path.basename(current_img_path)
    
    st.markdown(f"### Image {st.session_state.current_idx + 1} of {len(st.session_state.anno_queue)}: `{filename}`")
    
    # 1. AI Processing (Runs once per image)
    if st.session_state.current_ai_data is None:
        with st.spinner("🤖 AI is detecting faces and reading the caption..."):
            pil_img = Image.open(current_img_path)
            caption = get_caption_from_txt(current_txt_path)
            
            # Run MTCNN
            faces = detect_faces(pil_img)
            
            # Map Names with Gemini
            mapped_names = []
            if faces and caption:
                numbered_img = draw_numbered_boxes(pil_img, faces)
                mapped_names = map_faces_to_caption(numbered_img, caption)
            elif not faces and caption:
                # Fallback: No faces found, but we have a caption. Let Gemini just extract names.
                mapped_names = map_faces_to_caption(pil_img, caption)
                
            # Prepare Canvas initial data
            canvas_json = generate_fabric_json(faces)
            
            st.session_state.current_ai_data = {
                "caption": caption,
                "faces": faces,
                "mapped_names": mapped_names,
                "canvas_json": canvas_json,
                "manual_names": [] # For user-added names
            }
            st.rerun()

    # 2. Render the Review UI
    ai_data = st.session_state.current_ai_data
    pil_img = Image.open(current_img_path)
    orig_w, orig_h = pil_img.size
    
    # Determine canvas display size (scale down for UI, but keep aspect ratio)
    canvas_display_w = 700
    canvas_display_h = int(orig_h * (canvas_display_w / orig_w))

    st.info(f"**Caption:** {ai_data['caption']}")

    col1, col2 = st.columns([2, 1])

    with col1:
        st.write("✏️ **Draw, Move, or Delete Boxes** (Select a box and press Delete/Backspace to remove)")
        
        # The Canvas Component
        canvas_result = st_canvas(
            fill_color="rgba(0, 0, 0, 0)",
            stroke_width=3,
            stroke_color="#FF0000",
            background_image=pil_img,
            update_streamlit=True,
            height=canvas_display_h,
            width=canvas_display_w,
            drawing_mode="rect",
            initial_drawing=ai_data["canvas_json"],
            key=f"canvas_{st.session_state.current_idx}",
        )

    with col2:
        st.write("📋 **Map Names to Boxes**")
        
        # Extract current boxes from canvas
        current_boxes = []
        if canvas_result.json_data is not None:
            current_boxes = canvas_result.json_data["objects"]
            
        box_options = ["None"] + [f"Box {i+1}" for i in range(len(current_boxes))]
        
        # Form to handle mapping
        with st.form(key=f"map_form_{st.session_state.current_idx}"):
            final_mappings = {}
            
            # Combine AI names and Manual names
            all_names_to_map = ai_data["mapped_names"] + [{"name": n, "box_id": None} for n in ai_data["manual_names"]]
            
            for item in all_names_to_map:
                name = item["name"]
                ai_box_id = item.get("box_id")
                
                # Determine default index for the selectbox
                default_idx = 0
                if ai_box_id is not None and 1 <= ai_box_id <= len(current_boxes):
                    default_idx = ai_box_id
                    
                selected_box = st.selectbox(
                    f"Name: {name}",
                    options=box_options,
                    index=default_idx,
                    key=f"map_{name}"
                )
                
                if selected_box != "None":
                    # Convert "Box 1" to integer index 0
                    box_idx = int(selected_box.replace("Box ", "")) - 1
                    final_mappings[name] = box_idx

            st.divider()
            submit_btn = st.form_submit_button("💾 Save Annotations & Next", type="primary")

        # Allow user to manually add a name missed by Gemini
        st.write("➕ **Missed a name?**")
        new_name = st.text_input("Enter name:")
        if st.button("Add Name"):
            if new_name and new_name not in [n["name"] for n in ai_data["mapped_names"]] and new_name not in ai_data["manual_names"]:
                st.session_state.current_ai_data["manual_names"].append(new_name)
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
        scale_x = orig_w / canvas_display_w
        scale_y = orig_h / canvas_display_h
        
        # Generate the ImageNote templates
        for i, (name, box_idx) in enumerate(final_mappings.items()):
            box = current_boxes[box_idx]
            
            # Canvas coordinates are relative to canvas_display_w/h. Must scale up to original image size.
            true_x = int(box["left"] * scale_x)
            true_y = int(box["top"] * scale_y)
            # Fabric.js scales width/height if the user resized the box
            true_w = int(box["width"] * box.get("scaleX", 1) * scale_x)
            true_h = int(box["height"] * box.get("scaleY", 1) * scale_y)
            
            template = f"{{{{ImageNote|id={i+1}|x={true_x}|y={true_y}|w={true_w}|h={true_h}|dimx={orig_w}|dimy={orig_h}|style=2}}}}\n"
            template += f"{{{{ia|{name}}}}}\n"
            template += f"{{{{ImageNoteEnd|id={i+1}}}}}"
            wikitext_blocks.append(template)
            
        final_wikitext = "\n".join(wikitext_blocks)
        
        # Append to the .txt file
        append_annotations_to_txt(current_txt_path, final_wikitext)
        
        st.success("Saved!")
        st.session_state.current_idx += 1
        st.session_state.current_ai_data = None
        st.rerun()
