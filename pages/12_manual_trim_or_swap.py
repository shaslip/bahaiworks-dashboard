import streamlit as st
import os
import re
from PIL import Image

st.set_page_config(page_title="Manual Image Trimmer & Swapper", page_icon="✂️", layout="wide")

st.title("✂️ Manual Image Trimmer & 🔄 Swapper")

# --- Initialize Session State ---
if "image_queue" not in st.session_state:
    st.session_state.image_queue = []
if "multi_image_pages" not in st.session_state:
    st.session_state.multi_image_pages = {}

# --- Setup & Loading ---
st.sidebar.header("Configuration")
folder_path = st.sidebar.text_input("Images Folder Path", value="/home/sarah/Desktop/Projects/Bahai.works/English/images/")

# Create Tabs
tab1, tab2 = st.tabs(["✂️ Manual Trimmer", "🔄 Swap Misnamed Images"])

# ==========================================
# TAB 1: EXISTING MANUAL TRIMMER
# ==========================================
with tab1:
    if st.button("Load Images from Folder (Trimmer)"):
        if os.path.exists(folder_path):
            # Grab only image files
            valid_exts = ('.png', '.jpg', '.jpeg')
            images = sorted([f for f in os.listdir(folder_path) if f.lower().endswith(valid_exts)])
            st.session_state.image_queue = [os.path.join(folder_path, img) for img in images]
        else:
            st.error("Invalid folder path.")

    if st.session_state.image_queue:
        st.write(f"### {len(st.session_state.image_queue)} Images in Queue")
        
        # Iterate over a copy so we can safely remove items during the loop
        for img_path in list(st.session_state.image_queue):
            filename = os.path.basename(img_path)
            st.markdown(f"**{filename}**")
            
            # Spatial Layout: Left Col (Left Crop), Center Col (Top/Img/Bot), Right Col (Right Crop), Action Col
            col_left, col_center, col_right, col_action = st.columns([1, 4, 1, 1.5], vertical_alignment="center")
            
            with col_left:
                st.number_input("Left px", min_value=0, value=2, key=f"l_{img_path}")
                
            with col_center:
                st.number_input("Top px", min_value=0, value=2, key=f"t_{img_path}")
                # Display image. Using a reasonably constrained width so you can see the whole thing without scrolling
                st.image(img_path, width=600) 
                st.number_input("Bottom px", min_value=0, value=2, key=f"b_{img_path}")
                
            with col_right:
                st.number_input("Right px", min_value=0, value=2, key=f"r_{img_path}")
                
            with col_action:
                st.number_input("Rotate (° CW)", value=0.0, step=0.1, format="%.2f", key=f"rot_{img_path}")
                st.write("") # small spacer
                if st.button("🗑️ Skip / Remove", key=f"skip_{img_path}"):
                    st.session_state.image_queue.remove(img_path)
                    st.rerun()

            st.divider()

        # --- Processing Execution ---
        if st.button("🚀 Apply Crops & Save", type="primary"):
            for img_path in st.session_state.image_queue:
                t = st.session_state[f"t_{img_path}"]
                b = st.session_state[f"b_{img_path}"]
                l = st.session_state[f"l_{img_path}"]
                r = st.session_state[f"r_{img_path}"]
                rot = st.session_state[f"rot_{img_path}"]
                
                # Skip file operation if no change is requested
                if t == 0 and b == 0 and l == 0 and r == 0 and rot == 0.0:
                    continue 
                    
                img = Image.open(img_path)
                
                # 1. Apply Rotation First
                if rot != 0.0:
                    # PIL rotates counter-clockwise by default, so -rot makes positive inputs clockwise.
                    img = img.rotate(-rot, resample=Image.BICUBIC, expand=True, fillcolor="white")
                    
                # 2. Get dimensions AFTER rotation so bounds checks don't fail
                w, h = img.size
                
                # Validate bounds to prevent hard crashes
                if l + r >= w or t + b >= h:
                    st.error(f"Crop parameters exceed image dimensions for {os.path.basename(img_path)}. Skipped.")
                    continue
                    
                # PIL crop tuple format: (left, upper, right, lower)
                cropped_img = img.crop((l, t, w - r, h - b))
                cropped_img.save(img_path)
                
            st.success("Changes applied to queue. Originals overwritten.")
            st.session_state.image_queue = [] 
            st.rerun()

    elif folder_path and not st.session_state.image_queue:
        st.info("Trimmer queue is empty. Load a folder using the button above.")


# ==========================================
# TAB 2: SWAP MISNAMED IMAGES
# ==========================================
with tab2:
    st.write("Automatically detects pages with multiple images and allows you to reassign their filenames.")
    
    if st.button("Scan Folder for Multi-Image Pages"):
        if os.path.exists(folder_path):
            pages_dict = {}
            for filename in os.listdir(folder_path):
                if filename.lower().endswith('.txt'):
                    txt_path = os.path.join(folder_path, filename)
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # Look for the last parameter in the source template indicating the page number
                    # e.g., | source = {{bns|367|3}} -> captures '3'
                    match = re.search(r'\|\s*source\s*=\s*\{\{.*?\|(\d+)\}\}', content)
                    
                    if match:
                        page_num = match.group(1)
                        base_name = os.path.splitext(filename)[0]
                        
                        # Find the corresponding image file
                        img_path = None
                        for ext in ['.png', '.jpg', '.jpeg']:
                            potential_path = os.path.join(folder_path, base_name + ext)
                            if os.path.exists(potential_path):
                                img_path = potential_path
                                break

                        if img_path:
                            if page_num not in pages_dict:
                                pages_dict[page_num] = []
                            pages_dict[page_num].append(img_path)

            # Keep only pages that have > 1 image
            multi_image_pages = {int(p): imgs for p, imgs in pages_dict.items() if len(imgs) > 1}
            # Sort dict by page number ascending
            st.session_state.multi_image_pages = dict(sorted(multi_image_pages.items()))
            
            if not st.session_state.multi_image_pages:
                st.warning("No pages with multiple images found.")
            else:
                st.success(f"Found {len(st.session_state.multi_image_pages)} pages with multiple images.")
        else:
            st.error("Invalid folder path.")

    # Display UI for Swapping
    if st.session_state.multi_image_pages:
        for page, img_paths in st.session_state.multi_image_pages.items():
            st.markdown(f"### Page {page}")
            
            base_names = [os.path.basename(p) for p in img_paths]
            
            # Using a form per page so swaps happen atomically
            with st.form(key=f"form_page_{page}"):
                cols = st.columns(len(img_paths))
                selections = {}
                
                for i, img_path in enumerate(img_paths):
                    current_name = os.path.basename(img_path)
                    with cols[i]:
                        st.image(img_path, use_column_width=True)
                        
                        # The user selects the TRUE filename for the image displayed above
                        selections[current_name] = st.selectbox(
                            f"True filename",
                            options=base_names,
                            index=base_names.index(current_name),
                            key=f"swap_{page}_{current_name}"
                        )
                
                if st.form_submit_button("Apply File Name Changes"):
                    # Validate that the user didn't assign the same name to two different images
                    if len(set(selections.values())) != len(base_names):
                        st.error("Action blocked: You must select a unique filename for each image.")
                    else:
                        temp_map = {}
                        
                        # Pass 1: Rename to temporary names to prevent overwriting during swap chains
                        for orig_name, new_name in selections.items():
                            if orig_name != new_name:
                                orig_path = os.path.join(folder_path, orig_name)
                                temp_path = os.path.join(folder_path, f"temp_swap_{orig_name}")
                                os.rename(orig_path, temp_path)
                                temp_map[temp_path] = os.path.join(folder_path, new_name)
                                
                        # Pass 2: Rename from temporary to final target names
                        for temp_path, final_path in temp_map.items():
                            os.rename(temp_path, final_path)

                        if temp_map:
                            st.success(f"Successfully reassigned images on Page {page}! Please re-scan the folder.")
                        else:
                            st.info("No file names were changed.")
            st.divider()
