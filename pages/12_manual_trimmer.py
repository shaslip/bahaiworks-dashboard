import streamlit as st
import os
from PIL import Image

st.set_page_config(page_title="Manual Image Trimmer", page_icon="✂️", layout="wide")

st.title("✂️ Manual Image Trimmer")

# --- Initialize Session State ---
if "image_queue" not in st.session_state:
    st.session_state.image_queue = []

# --- Setup & Loading ---
st.sidebar.header("Configuration")
folder_path = st.sidebar.text_input("Images Folder Path", value="/home/sarah/Desktop/Projects/Bahai.works/English/images/")

if st.sidebar.button("Load Images from Folder"):
    if os.path.exists(folder_path):
        # Grab only image files
        valid_exts = ('.png', '.jpg', '.jpeg')
        images = sorted([f for f in os.listdir(folder_path) if f.lower().endswith(valid_exts)])
        st.session_state.image_queue = [os.path.join(folder_path, img) for img in images]
    else:
        st.sidebar.error("Invalid folder path.")

# --- UI Generation ---
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
                # expand=True prevents corner clipping. fillcolor assumes a white background for document scans.
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

elif folder_path:
    st.info("Queue is empty. Load a folder from the sidebar.")
