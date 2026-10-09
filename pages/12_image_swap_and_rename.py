import streamlit as st
import os
import re
import subprocess

st.set_page_config(page_title="Image Swapper & Renamer", page_icon="🔄", layout="wide")

st.title("🔄 Image Swapper & Renamer")

# --- Helper Functions for Template Parsing ---
def extract_page_number(content):
    # Matches {{misc|Title|PageNumber|...}}
    match_misc = re.search(r'\|\s*source\s*=\s*\{\{misc\|[^|]*\|(-?\d+)', content, re.IGNORECASE)
    if match_misc:
        return match_misc.group(1)
    # Fallback to standard {{template|...|PageNumber}}
    match_standard = re.search(r'\|\s*source\s*=\s*\{\{.*?\|(-?\d+)\}\}', content)
    if match_standard:
        return match_standard.group(1)
    return None

def replace_page_number(content, new_page):
    if re.search(r'\|\s*source\s*=\s*\{\{misc\|', content, re.IGNORECASE):
        return re.sub(r'(\|\s*source\s*=\s*\{\{misc\|[^|]*\|)-?\d+', lambda m: f"{m.group(1)}{new_page}", content, flags=re.IGNORECASE)
    else:
        return re.sub(r'(\|\s*source\s*=\s*\{\{.*?\|)-?\d+(\}\})', lambda m: f"{m.group(1)}{new_page}{m.group(2)}", content)

# --- Initialize Session State ---
if "multi_image_pages" not in st.session_state:
    st.session_state.multi_image_pages = {}

# --- Setup & Loading ---
st.sidebar.header("Configuration")
folder_path = st.sidebar.text_input("Images Folder Path", value="/home/sarah/Desktop/Projects/Bahai.works/English/images/")

# Create Tabs
tab1, tab2, tab3 = st.tabs(["🔄 Swap Misnamed Images", "📝 Rename & Caption", "🗂️ Bulk Rename"])

# ==========================================
# TAB 1: SWAP MISNAMED IMAGES
# ==========================================
def on_swap_change(page, changed_img, base_names):
    """Callback to automatically swap the other image's selectbox value."""
    new_val = st.session_state[f"swap_{page}_{changed_img}"]
    old_val = st.session_state[f"prev_swap_{page}_{changed_img}"]
    
    if new_val != old_val:
        for img in base_names:
            if img != changed_img and st.session_state.get(f"swap_{page}_{img}") == new_val:
                # Assign the old value to the image that previously held the new value
                st.session_state[f"swap_{page}_{img}"] = old_val
                st.session_state[f"prev_swap_{page}_{img}"] = old_val
                break
        st.session_state[f"prev_swap_{page}_{changed_img}"] = new_val

with tab1:
    st.write("Automatically detects pages with multiple images and allows you to reassign their filenames.")
    
    # --- Check for Long Captions (Runs automatically if folder is valid) ---
    if os.path.exists(folder_path):
        missing_captions_by_page = {}
        for filename in os.listdir(folder_path):
            if filename.lower().endswith('.txt'):
                txt_path = os.path.join(folder_path, filename)
                try:
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if "[CAPTION TOO LONG - INSERT MANUALLY]" in content:
                            page_val = extract_page_number(content)
                            page_key = int(page_val) if page_val else "Unknown"
                            
                            if page_key not in missing_captions_by_page:
                                missing_captions_by_page[page_key] = []
                            missing_captions_by_page[page_key].append(filename)
                except Exception:
                    pass
                    
        if missing_captions_by_page:
            st.error("🚨 **ACTION REQUIRED: The following files have missing/truncated captions and need manual editing:**")
            
            sorted_pages = sorted(missing_captions_by_page.keys(), key=lambda x: (isinstance(x, str), x))
            
            for page in sorted_pages:
                page_label = f"Page {page}" if isinstance(page, int) else "Unknown Page"
                st.markdown(f"**{page_label}:**")
                
                for mc in missing_captions_by_page[page]:
                    col_name, col_btn = st.columns([4, 1])
                    with col_name:
                        st.write(f"`{mc}`")
                    with col_btn:
                        if st.button("📝 Open in Kate", key=f"kate_{mc}"):
                            full_path = os.path.join(folder_path, mc)
                            subprocess.Popen(['kate', full_path])
                st.write("")
            st.divider()
    # -----------------------------------------------------------------------

    if st.button("Scan Folder for Multi-Image Pages"):
        # Clear previous swap states to prevent staleness
        for key in list(st.session_state.keys()):
            if key.startswith("swap_") or key.startswith("prev_swap_"):
                del st.session_state[key]
                
        if os.path.exists(folder_path):
            pages_dict = {}
            for filename in os.listdir(folder_path):
                if filename.lower().endswith('.txt'):
                    txt_path = os.path.join(folder_path, filename)
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # Look for the page number in the source template
                    page_num = extract_page_number(content)
                    
                    if page_num:
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
        # Pre-filter to remove pages where files were deleted in background
        valid_pages = {}
        for page, img_paths in st.session_state.multi_image_pages.items():
            valid_paths = [p for p in img_paths if os.path.exists(p)]
            if len(valid_paths) > 1:
                valid_pages[page] = valid_paths
        st.session_state.multi_image_pages = valid_pages

        for page, img_paths in st.session_state.multi_image_pages.items():
            base_names = [os.path.basename(p) for p in img_paths]
            
            st.markdown(f"### Page {page}")
            
            # Initialize session state for the selectboxes
            for img in base_names:
                key = f"swap_{page}_{img}"
                prev_key = f"prev_swap_{page}_{img}"
                if key not in st.session_state:
                    st.session_state[key] = img
                if prev_key not in st.session_state:
                    st.session_state[prev_key] = img
            
            # Replaced st.form with standard columns to allow real-time callbacks for auto-swapping
            # Chunk images into rows of 2 to prevent overcrowding
            for i in range(0, len(img_paths), 2):
                cols = st.columns(2)
                chunk = img_paths[i:i+2]
                
                for j, img_path in enumerate(chunk):
                    current_name = os.path.basename(img_path)
                    with cols[j]:
                        st.image(img_path, width='stretch')
                        
                        # The user selects the TRUE filename for the image displayed above
                        st.selectbox(
                            "True filename",
                            options=base_names,
                            key=f"swap_{page}_{current_name}",
                            on_change=on_swap_change,
                            args=(page, current_name, base_names)
                        )
            
            if st.button("Apply File Name Changes", key=f"apply_swap_{page}"):
                selections = {img: st.session_state[f"swap_{page}_{img}"] for img in base_names}
                
                # Validate that the user didn't assign the same name to two different images
                if len(set(selections.values())) != len(base_names):
                    st.error("Action blocked: You must select a unique filename for each image.")
                else:
                    temp_map = {}
                    
                    # Pass 1: Rename to temporary names to prevent overwriting during swap chains
                    for orig_name, new_name in selections.items():
                        if orig_name != new_name:
                            orig_path = os.path.join(folder_path, orig_name)
                            if not os.path.exists(orig_path):
                                st.error(f"File {orig_name} was missing. Aborting swap for this page.")
                                temp_map = {}
                                break
                                
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

# ==========================================
# TAB 2: RENAME & CAPTION
# ==========================================
with tab2:
    st.write("Find all images on a specific page to quickly rename them and update their captions.")
    
    target_page = st.text_input("Enter Page Number:")
    
    if target_page and os.path.exists(folder_path):
        page_files = []
        
        for filename in os.listdir(folder_path):
            if filename.lower().endswith('.txt'):
                txt_path = os.path.join(folder_path, filename)
                try:
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    page_val = extract_page_number(content)
                    if page_val and page_val == target_page.strip():
                        base_name = os.path.splitext(filename)[0]
                        current_page_val = page_val
                        
                        # Simplest possible regex: grab everything after "=" until the next "|"
                        cap_match = re.search(r'\|\s*caption\s*=([^|]*)', content)
                        existing_caption = cap_match.group(1).strip() if cap_match else ""
                        
                        img_path = None
                        for ext in ['.png', '.jpg', '.jpeg', '.PNG', '.JPG', '.JPEG']:
                            p = os.path.join(folder_path, base_name + ext)
                            if os.path.exists(p):
                                img_path = p
                                break
                                
                        if img_path:
                            page_files.append((base_name, txt_path, img_path, existing_caption, current_page_val))
                except Exception:
                    pass
                    
        if not page_files:
            st.info(f"No images found for page {target_page}.")
        else:
            with st.form(key=f"rename_form_{target_page}"):
                updates = {}
                
                for base_name, txt_path, img_path, existing_caption, current_page_val in page_files:
                    col_img, col_name, col_cap, col_page = st.columns([1.5, 2.5, 3, 0.5])
                    
                    with col_img:
                        st.image(img_path, width=150)
                    with col_name:
                        new_name = st.text_input("New Filename (no ext)", value=base_name, key=f"name_{base_name}")
                    with col_cap:
                        new_caption = st.text_area("Caption", value=existing_caption, key=f"cap_{base_name}")
                    with col_page:
                        new_page = st.text_input("Page #", value=current_page_val, key=f"page_{base_name}")
                        
                    updates[base_name] = {
                        "txt_path": txt_path,
                        "img_path": img_path,
                        "new_name": new_name,
                        "new_caption": new_caption,
                        "new_page": new_page
                    }
                    st.divider()
                    
                if st.form_submit_button("Save Changes"):
                    new_names = [data["new_name"].strip().replace(" ", "_") for data in updates.values()]
                    new_pages = [data["new_page"].strip() for data in updates.values()]
                    
                    if any(name == "" for name in new_names):
                        st.error("Action blocked: Filenames cannot be blank.")
                    elif len(new_names) != len(set(new_names)):
                        st.error("Action blocked: You entered duplicate filenames. Each image must have a unique name.")
                    elif any(page == "" for page in new_pages):
                        st.error("Action blocked: Page numbers cannot be blank.")
                    else:
                        # COLLISION CHECK
                        collision_detected = False
                        for base_name, data in updates.items():
                            n_name = data["new_name"].strip().replace(" ", "_")
                            if n_name != base_name:
                                ext = os.path.splitext(data["img_path"])[1]
                                check_txt = os.path.join(folder_path, f"{n_name}.txt")
                                check_img = os.path.join(folder_path, f"{n_name}{ext}")
                                
                                # Block if target exists AND it isn't the file we are currently modifying
                                if (os.path.exists(check_txt) and check_txt != data["txt_path"]) or (os.path.exists(check_img) and check_img != data["img_path"]):
                                    st.error(f"Action blocked: The filename '{n_name}' already exists. Please choose a different name.")
                                    collision_detected = True
                                    break
                        
                        if not collision_detected:
                            for base_name, data in updates.items():
                                # Existence check right before modifying
                                if not os.path.exists(data["txt_path"]) or not os.path.exists(data["img_path"]):
                                st.warning(f"Skipped {base_name}: File was deleted in the background.")
                                continue
                                
                            n_name = data["new_name"].strip().replace(" ", "_")
                            n_cap = data["new_caption"].strip()
                            n_page = data["new_page"].strip()
                            
                            with open(data["txt_path"], 'r', encoding='utf-8') as f:
                                content = f.read()
                                
                            # Replaces everything up to the next pipe with the new caption
                            content = re.sub(r'(\|\s*caption\s*=)[^|]*', lambda m, cap=n_cap: f"{m.group(1)} {cap}\n", content)
                            
                            # Updates the page number safely
                            content = replace_page_number(content, n_page)
                                
                            new_txt_path = os.path.join(folder_path, f"{n_name}.txt")
                            with open(new_txt_path, 'w', encoding='utf-8') as f:
                                f.write(content)
                                
                            if n_name != base_name:
                                ext = os.path.splitext(data["img_path"])[1]
                                new_img_path = os.path.join(folder_path, f"{n_name}{ext}")
                                os.rename(data["img_path"], new_img_path)
                                
                                if data["txt_path"] != new_txt_path and os.path.exists(data["txt_path"]):
                                    os.remove(data["txt_path"])
                                    
                        st.success("Files updated successfully!")


# ==========================================
# TAB 3: BULK RENAME
# ==========================================
with tab3:
    st.write("Apply a single base filename and caption to all images on a specific page. Files will be numbered sequentially (e.g., -1, -2).")
    
    target_page_bulk = st.text_input("Enter Page Number:", key="bulk_target_page")
    
    if target_page_bulk and os.path.exists(folder_path):
        page_files = []
        
        for filename in os.listdir(folder_path):
            if filename.lower().endswith('.txt'):
                txt_path = os.path.join(folder_path, filename)
                try:
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    page_val = extract_page_number(content)
                    if page_val and page_val == target_page_bulk.strip():
                        base_name = os.path.splitext(filename)[0]
                        
                        img_path = None
                        for ext in ['.png', '.jpg', '.jpeg', '.PNG', '.JPG', '.JPEG']:
                            p = os.path.join(folder_path, base_name + ext)
                            if os.path.exists(p):
                                img_path = p
                                break
                                
                        if img_path:
                            page_files.append((base_name, txt_path, img_path))
                except Exception:
                    pass
                    
        if not page_files:
            st.info(f"No images found for page {target_page_bulk}.")
        else:
            page_files.sort(key=lambda x: x[0])
            st.write(f"### Found {len(page_files)} images")
            
            cols = st.columns(min(len(page_files), 6))
            for idx, (_, _, img_path) in enumerate(page_files):
                with cols[idx % len(cols)]:
                    st.image(img_path, width='stretch')
                    
            with st.form(key=f"bulk_rename_form_{target_page_bulk}"):
                bulk_name = st.text_input("Base Filename (spaces will be converted to underscores)")
                bulk_caption = st.text_area("Caption (applied to all images)")
                
                if st.form_submit_button("Apply Bulk Rename & Caption"):
                    if not bulk_name.strip():
                        st.error("Action blocked: Base filename cannot be blank.")
                    else:
                        clean_base = bulk_name.strip().replace(" ", "_")
                        
                        temp_files = []
                        # Validate all files exist before starting any renames
                        missing_files = False
                        for _, txt_path, img_path in page_files:
                            if not os.path.exists(txt_path) or not os.path.exists(img_path):
                                missing_files = True
                                break
                                
                        if missing_files:
                            st.error("Action blocked: Some files were deleted in the background. Please refresh the page.")
                        else:
                            # --- COLLISION CHECK ---
                            collision = False
                            for i, (base_name, txt_path, img_path) in enumerate(page_files, start=1):
                                orig_ext = os.path.splitext(img_path)[1]
                                n_name = f"{clean_base}-{i}"
                                check_txt = os.path.join(folder_path, f"{n_name}.txt")
                                check_img = os.path.join(folder_path, f"{n_name}{orig_ext}")
                                
                                # Block if target exists AND it isn't the file we are currently modifying
                                if (os.path.exists(check_txt) and check_txt != txt_path) or (os.path.exists(check_img) and check_img != img_path):
                                    st.error(f"Action blocked: Target file '{n_name}' already exists. Please choose a different base name to prevent overwriting.")
                                    collision = True
                                    break
                                    
                            if not collision:
                                for i, (base_name, txt_path, img_path) in enumerate(page_files, start=1):
                                    # Grab the original extension BEFORE appending .tmp
                                    orig_ext = os.path.splitext(img_path)[1]
                                    
                                    temp_img = img_path + ".tmp"
                                temp_txt = txt_path + ".tmp"
                                os.rename(img_path, temp_img)
                                os.rename(txt_path, temp_txt)
                                
                                # Store the orig_ext in the tuple
                                temp_files.append((base_name, temp_txt, temp_img, i, orig_ext))
                            
                            for base_name, temp_txt, temp_img, i, orig_ext in temp_files:
                                n_name = f"{clean_base}-{i}"
                                
                                with open(temp_txt, 'r', encoding='utf-8') as f:
                                    content = f.read()
                                    
                                content = re.sub(r'(\|\s*caption\s*=)[^|]*', lambda m, cap=bulk_caption.strip(): f"{m.group(1)} {cap}\n", content)
                                    
                                new_txt_path = os.path.join(folder_path, f"{n_name}.txt")
                                with open(new_txt_path, 'w', encoding='utf-8') as f:
                                    f.write(content)
                                    
                                # Use the original extension we saved earlier
                                new_img_path = os.path.join(folder_path, f"{n_name}{orig_ext}")
                                os.rename(temp_img, new_img_path)
                                os.remove(temp_txt)
                                    
                            st.success(f"Successfully bulk-renamed {len(page_files)} files!")
