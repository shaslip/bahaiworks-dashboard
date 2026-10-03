import streamlit as st
import os
import sys
import re
import json
import requests
import time

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from src.mediawiki_uploader import get_category_files, fetch_wikitext, get_image_url, upload_to_mediawiki
from src.gemini_processor import suggest_blind_categories, filter_fuzzy_categories
from src.category_manager import get_fuzzy_candidates, resolve_aliases

MEDIA_API_URL = 'https://bahai.media/api.php'

st.set_page_config(page_title="Auto-Categorization Tool", page_icon="🗂️", layout="wide")

# --- State Initialization ---
if "ac_step" not in st.session_state:
    st.session_state.ac_step = 0  # 0: Setup, 1: Review/Edit, 2: Sweeper Done
if "ac_data" not in st.session_state:
    st.session_state.ac_data = {}
if "ac_files_to_process" not in st.session_state:
    st.session_state.ac_files_to_process = []

def reset_app():
    st.session_state.ac_step = 0
    st.session_state.ac_data = {}
    st.session_state.ac_files_to_process = []

def get_caption_from_text(content):
    if not content: return ""
    match = re.search(r'\|\s*caption\s*=\s*(.*?)\n\|', content, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

def append_categories_to_wikitext(wikitext, new_categories):
    appended = False
    clean_text = wikitext.strip()
    
    for cat in new_categories:
        cat = cat.strip()
        if not cat: continue
        pattern = r'\[\[Category:\s*' + re.escape(cat) + r'\s*\]\]'
        if not re.search(pattern, clean_text, re.IGNORECASE):
            if not appended and not clean_text.endswith("]]"):
                clean_text += "\n"
            clean_text += f"\n[[Category:{cat}]]"
            appended = True
            
    return clean_text, appended

def generate_ai_data(files_to_process, context_mapping):
    """Runs the AI pipeline but DOES NOT upload. Saves to session state."""
    session = requests.Session()
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, file_title in enumerate(files_to_process):
        status_text.text(f"AI Processing {i+1}/{len(files_to_process)}: {file_title}")
        context = context_mapping.get(file_title, "")
        
        wikitext, _ = fetch_wikitext(file_title, session=session, api_url=MEDIA_API_URL)
        caption = get_caption_from_text(wikitext)
        
        if not caption:
            st.session_state.ac_data[file_title] = {"error": "No caption found", "wikitext": wikitext}
            progress_bar.progress((i + 1) / len(files_to_process))
            continue
            
        blind_suggestions = suggest_blind_categories(caption, context=context)
        fuzzy_candidates = get_fuzzy_candidates(blind_suggestions, limit_per_suggestion=5)
        raw_final_picks = filter_fuzzy_categories(caption, fuzzy_candidates, context=context)
        final_picks = resolve_aliases(raw_final_picks)
        image_url = get_image_url(file_title, session=session, api_url=MEDIA_API_URL)
        
        st.session_state.ac_data[file_title] = {
            "caption": caption,
            "1_blind_suggestions": blind_suggestions,
            "2_fuzzy_candidates": fuzzy_candidates,
            "3_final_picks": final_picks,
            "image_url": image_url,
            "wikitext": wikitext
        }
        progress_bar.progress((i + 1) / len(files_to_process))
        
    status_text.success("AI Processing complete! Ready for review.")
    time.sleep(1)

# ==========================================
# STEP 0: SETUP & FETCH
# ==========================================
if st.session_state.ac_step == 0:
    st.title("🗂️ Auto-Categorization Tool")
    st.markdown("Extract captions, generate fuzzy category matches, filter with AI, and review before saving.")

    tab1, tab2, tab3 = st.tabs(["📄 Single File", "📁 Single Category", "📚 Sequential Categories (Sweeper)"])

    # --- TAB 1: SINGLE FILE ---
    with tab1:
        file_input = st.text_input("File Name", placeholder="e.g. File:Race_Unity_Day_in_Austin_Texas.png")
        context_input1 = st.text_input("Context (Optional)", placeholder="e.g. The American Bahá'í 1974 USA", key="ctx1")
        
        if st.button("Process Single File", type="primary"):
            if not file_input: st.warning("Please enter a file name."); st.stop()
            if not file_input.lower().startswith("file:"): file_input = "File:" + file_input
            
            st.session_state.ac_files_to_process = [file_input]
            context_map = {file_input: context_input1.strip()}
            
            with st.spinner("Running AI Analysis..."):
                generate_ai_data(st.session_state.ac_files_to_process, context_map)
            st.session_state.ac_step = 1
            st.rerun()

    # --- TAB 2: SINGLE CATEGORY ---
    with tab2:
        category_input = st.text_input("Category Name", placeholder="e.g. Category:The American Bahá'í Vol 5 No 8")
        context_input2 = st.text_input("Context (Optional)", placeholder="e.g. The American Bahá'í 1974 USA", key="ctx2")
        
        if st.button("Process Single Category", type="primary"):
            if not category_input: st.warning("Please enter a category name."); st.stop()
                
            with st.spinner(f"Fetching files from {category_input}..."):
                files = get_category_files(category_input, api_url=MEDIA_API_URL)
                
            if not files: st.error("No files found."); st.stop()
                
            st.session_state.ac_files_to_process = files
            active_context = context_input2.strip() if context_input2.strip() else category_input
            context_map = {f: active_context for f in files}
            
            with st.spinner("Running AI Analysis..."):
                generate_ai_data(st.session_state.ac_files_to_process, context_map)
            st.session_state.ac_step = 1
            st.rerun()

    # --- TAB 3: SWEEPER (Fully Automatic) ---
    with tab3:
        st.info("Runs fully automatically in the background. Skips the review step and uploads directly.")
        col1, col2 = st.columns([3, 1])
        with col1: seq_category_input = st.text_input("Starting Category", placeholder="e.g. Category:AB Volume 5 No 1")
        with col2: target_count = st.number_input("Target Sequences", min_value=1, max_value=100, value=12)
        context_input3 = st.text_input("Context (Optional)", key="ctx3")
            
        if st.button("Run Sweeper", type="primary"):
            if not seq_category_input: st.warning("Please enter a starting category."); st.stop()
            match = re.search(r'^(.*?)(\d+)(\D*)$', seq_category_input)
            if not match: st.error("Could not find a number in the category name to sequence."); st.stop()
                
            prefix, num_str, suffix = match.groups()
            current_num = int(num_str)
            all_files = []
            context_map = {}
            
            with st.spinner("Fetching files across sequences..."):
                for _ in range(target_count):
                    current_cat = f"{prefix}{current_num}{suffix}"
                    files = get_category_files(current_cat, api_url=MEDIA_API_URL)
                    if files:
                        all_files.extend(files)
                        active_context = context_input3.strip() if context_input3.strip() else current_cat
                        for f in files: context_map[f] = active_context
                    current_num += 1
                    time.sleep(0.5)
                    
            if not all_files: st.error("No files found."); st.stop()
            
            # Sweeper runs AI and uploads immediately
            with st.spinner("Running AI and uploading..."):
                generate_ai_data(all_files, context_map)
                
                session = requests.Session()
                for file_title, data in st.session_state.ac_data.items():
                    if "error" in data: continue
                    new_cats = data.get("3_final_picks", [])
                    if new_cats:
                        new_wikitext, changed = append_categories_to_wikitext(data["wikitext"], new_cats)
                        if changed:
                            upload_to_mediawiki(file_title, new_wikitext, "Auto-categorized via AI (Fuzzy Matching)", session, MEDIA_API_URL)
            
            st.session_state.ac_step = 2
            st.rerun()

# ==========================================
# STEP 1: REVIEW & EDIT (Interactive)
# ==========================================
elif st.session_state.ac_step == 1:
    st.title("📝 Review & Edit Categories")
    
    # Header buttons
    col1, col2, col3 = st.columns([1, 1, 4])
    with col1:
        if st.button("Cancel / Start Over"): reset_app(); st.rerun()
    with col2:
        # Generate JSON for download
        json_data = json.dumps(st.session_state.ac_data, indent=4, ensure_ascii=False)
        st.download_button(label="⬇️ Download JSON", data=json_data, file_name="auto_cat_results.json", mime="application/json")

    st.divider()
    
    # Build UI for each file
    for file_title, data in st.session_state.ac_data.items():
        st.markdown(f"### [{file_title}](https://bahai.media/{file_title.replace(' ', '_')})")
        
        if "error" in data:
            st.error(data["error"])
            st.divider()
            continue
            
        col_img, col_info = st.columns([1, 1.5])
        
        with col_img:
            if data.get("image_url"):
                st.image(data["image_url"], use_container_width=True)
            st.info(f"**Caption:** {data['caption']}")
            
        with col_info:
            with st.expander("View AI Reasoning (Blind & Fuzzy)"):
                st.write("**Blind Suggestions:**", data["1_blind_suggestions"])
                st.write("**Fuzzy Candidates:**", data["2_fuzzy_candidates"])
                
            # Editable text input pre-filled with AI's final picks
            default_cats = ", ".join(data["3_final_picks"])
            st.text_input(
                "Final Categories (Comma separated)", 
                value=default_cats, 
                key=f"edit_cats_{file_title}",
                help="Add, remove, or edit categories here. Leave blank to skip this file."
            )
            
        st.divider()
        
    # Upload Button
    if st.button("🚀 Save & Upload to Bahai.media", type="primary", use_container_width=True):
        session = requests.Session()
        success_count = 0
        
        progress_bar = st.progress(0)
        status = st.empty()
        
        files_list = list(st.session_state.ac_data.keys())
        for i, file_title in enumerate(files_list):
            data = st.session_state.ac_data[file_title]
            if "error" in data: continue
                
            # Read the user's edited categories from the session state
            user_input = st.session_state.get(f"edit_cats_{file_title}", "")
            final_cats = [c.strip() for c in user_input.split(",") if c.strip()]
            
            if final_cats:
                status.text(f"Uploading {file_title}...")
                new_wikitext, changed = append_categories_to_wikitext(data["wikitext"], final_cats)
                if changed:
                    try:
                        upload_to_mediawiki(file_title, new_wikitext, "Categorized via AI Tool", session, MEDIA_API_URL)
                        success_count += 1
                    except Exception as e:
                        st.error(f"Failed to upload {file_title}: {e}")
            
            progress_bar.progress((i + 1) / len(files_list))
            
        st.success(f"Upload complete! Updated {success_count} files.")
        time.sleep(2)
        reset_app()
        st.rerun()

# ==========================================
# STEP 2: SWEEPER DONE
# ==========================================
elif st.session_state.ac_step == 2:
    st.title("✅ Sweeper Complete")
    st.success("All sequences have been processed and uploaded.")
    
    json_data = json.dumps(st.session_state.ac_data, indent=4, ensure_ascii=False)
    st.download_button(label="⬇️ Download JSON Results", data=json_data, file_name="auto_cat_results.json", mime="application/json", type="primary")
    
    if st.button("Start Over"):
        reset_app()
        st.rerun()
