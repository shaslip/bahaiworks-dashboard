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
from src.category_manager import get_fuzzy_candidates

MEDIA_API_URL = 'https://bahai.media/api.php'

st.set_page_config(page_title="Auto-Categorization Tool", page_icon="🗂️", layout="wide")

def get_caption_from_text(content):
    if not content: return ""
    match = re.search(r'\|\s*caption\s*=\s*(.*?)\n\|', content, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

def append_categories_to_wikitext(wikitext, new_categories):
    """Appends new categories to the wikitext, avoiding duplicates."""
    appended = False
    clean_text = wikitext.strip()
    
    for cat in new_categories:
        # Check if category already exists (case-insensitive, handles spacing)
        pattern = r'\[\[Category:\s*' + re.escape(cat) + r'\s*\]\]'
        if not re.search(pattern, clean_text, re.IGNORECASE):
            # Ensure there's a blank line before the first appended category if not already at the end of a category block
            if not appended and not clean_text.endswith("]]"):
                clean_text += "\n"
            clean_text += f"\n[[Category:{cat}]]"
            appended = True
            
    return clean_text, appended

def process_files(files_to_process, context_mapping, show_ui=True):
    """
    Core processing loop. 
    files_to_process: list of filenames
    context_mapping: dict mapping filename -> context (e.g. the category it came from)
    show_ui: whether to render the image and results to the Streamlit UI
    """
    results = {}
    session = requests.Session()
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    success_count = 0
    skip_count = 0
    
    for i, file_title in enumerate(files_to_process):
        status_text.text(f"Processing {i+1}/{len(files_to_process)}: {file_title}")
        context = context_mapping.get(file_title, "")
        
        wikitext, _ = fetch_wikitext(file_title, session=session, api_url=MEDIA_API_URL)
        caption = get_caption_from_text(wikitext)
        
        if not caption:
            results[file_title] = {"error": "No caption found"}
            skip_count += 1
            progress_bar.progress((i + 1) / len(files_to_process))
            continue
            
        # 1. Blind Suggestions
        blind_suggestions = suggest_blind_categories(caption, context=context)
        
        # 2. Fuzzy Search
        fuzzy_candidates = get_fuzzy_candidates(blind_suggestions, limit_per_suggestion=5)
        
        # 3. AI Filter
        final_picks = filter_fuzzy_categories(caption, fuzzy_candidates, context=context)
        
        # 4. Save to Wiki
        if final_picks:
            new_wikitext, changed = append_categories_to_wikitext(wikitext, final_picks)
            if changed:
                try:
                    upload_to_mediawiki(
                        title=file_title,
                        content=new_wikitext,
                        summary="Auto-categorized via AI (Fuzzy Matching)",
                        session=session,
                        api_url=MEDIA_API_URL
                    )
                    success_count += 1
                except Exception as e:
                    st.error(f"Failed to upload {file_title}: {e}")
            else:
                skip_count += 1 # Categories were already present
        else:
            skip_count += 1 # No categories picked
            
        # 5. Record Results
        results[file_title] = {
            "caption": caption,
            "1_blind_suggestions": blind_suggestions,
            "2_fuzzy_candidates": fuzzy_candidates,
            "3_final_picks": final_picks
        }
        
        # 6. Render UI (if applicable)
        if show_ui:
            image_url = get_image_url(file_title, session=session, api_url=MEDIA_API_URL)
            with st.container(border=True):
                col1, col2 = st.columns([1, 1.5])
                with col1:
                    st.markdown(f"**[{file_title}](https://bahai.media/{file_title.replace(' ', '_')})**")
                    if image_url:
                        st.image(image_url, use_container_width=True)
                    st.info(f"**Caption:** {caption}")
                with col2:
                    st.write("**1. Blind Suggestions:**", blind_suggestions)
                    st.write("**2. Final AI Picks Applied:**")
                    if final_picks:
                        for cat in final_picks:
                            st.success(f"✅ [[Category:{cat}]]")
                    else:
                        st.warning("No categories applied.")
                        
        progress_bar.progress((i + 1) / len(files_to_process))
        
    status_text.success(f"Processing complete! Updated {success_count} files. Skipped {skip_count} files.")
    
    # Save JSON
    output_file = os.path.join(project_root, "auto_cat_results.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=4, ensure_ascii=False)
        
    with open(output_file, "rb") as file:
        st.download_button(
            label="⬇️ Download JSON Results",
            data=file,
            file_name="auto_cat_results.json",
            mime="application/json",
            type="primary"
        )

# ==========================================
# UI & TAB ROUTING
# ==========================================
st.title("🗂️ Auto-Categorization Tool")
st.markdown("Extract captions, generate fuzzy category matches, filter with AI, and save directly to Bahai.media.")

tab1, tab2, tab3 = st.tabs(["📄 Single File", "📁 Single Category", "📚 Sequential Categories (Sweeper)"])

# --- TAB 1: SINGLE FILE ---
with tab1:
    file_input = st.text_input("File Name", placeholder="e.g. File:Race_Unity_Day_in_Austin_Texas.png")
    context_input = st.text_input("Context (Optional)", placeholder="e.g. The American Bahá'í Vol 5 No 8", help="Providing context helps the AI make better decisions.")
    
    if st.button("Run Single File", type="primary"):
        if not file_input:
            st.warning("Please enter a file name.")
            st.stop()
            
        if not file_input.lower().startswith("file:"):
            file_input = "File:" + file_input
            
        files = [file_input]
        context_map = {file_input: context_input}
        
        with st.spinner("Processing..."):
            process_files(files, context_map, show_ui=True)

# --- TAB 2: SINGLE CATEGORY ---
with tab2:
    category_input = st.text_input("Category Name", placeholder="e.g. Category:The American Bahá'í Vol 5 No 8")
    
    if st.button("Run Single Category", type="primary"):
        if not category_input:
            st.warning("Please enter a category name.")
            st.stop()
            
        with st.spinner(f"Fetching files from {category_input}..."):
            files = get_category_files(category_input, api_url=MEDIA_API_URL)
            
        if not files:
            st.error("No files found in this category.")
            st.stop()
            
        st.info(f"Found {len(files)} files. Processing...")
        context_map = {f: category_input for f in files}
        process_files(files, context_map, show_ui=True)

# --- TAB 3: SEQUENTIAL CATEGORIES (SWEEPER) ---
with tab3:
    st.info("Runs fully automatically in the background. Does not render images to the screen to save memory.")
    col1, col2 = st.columns([3, 1])
    with col1:
        seq_category_input = st.text_input("Starting Category", placeholder="e.g. Category:AB Volume 5 No 1")
    with col2:
        target_count = st.number_input("Target Sequences (e.g. next 12 issues)", min_value=1, max_value=100, value=12)
        
    if st.button("Run Sweeper", type="primary"):
        if not seq_category_input:
            st.warning("Please enter a starting category.")
            st.stop()
            
        match = re.search(r'^(.*?)(\d+)(\D*)$', seq_category_input)
        if not match:
            st.error("Could not find a number in the category name to sequence.")
            st.stop()
            
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
                    for f in files:
                        context_map[f] = current_cat
                current_num += 1
                time.sleep(0.5) # Be polite to API
                
        if not all_files:
            st.error("No files found in any of the sequenced categories.")
            st.stop()
            
        st.info(f"Found {len(all_files)} total files across {target_count} categories. Processing...")
        
        # Run in headless mode (show_ui=False)
        process_files(all_files, context_map, show_ui=False)
