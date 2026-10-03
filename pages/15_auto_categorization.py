import streamlit as st
import os
import sys
import re
import json

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from src.mediawiki_uploader import get_category_files, fetch_wikitext
from src.gemini_processor import suggest_blind_categories, filter_fuzzy_categories
from src.category_manager import get_fuzzy_candidates

MEDIA_API_URL = 'https://bahai.media/api.php'

st.set_page_config(page_title="Batch Auto-Categorization Test", page_icon="🗂️", layout="wide")

def get_caption_from_text(content):
    if not content: return ""
    match = re.search(r'\|\s*caption\s*=\s*(.*?)\n\|', content, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

st.title("🗂️ Batch Auto-Categorization Evaluation")
st.markdown("Process an entire category and output the results to a JSON file for evaluation.")

category_input = st.text_input("Enter a Category Name", placeholder="e.g. Category:The American Bahá'í Vol 5 No 8")

if st.button("🧪 Run Batch Evaluation", type="primary"):
    if not category_input:
        st.warning("Please enter a category name.")
        st.stop()

    with st.spinner(f"Fetching files from {category_input}..."):
        files = get_category_files(category_input, api_url=MEDIA_API_URL)
        
    if not files:
        st.error("No files found in this category.")
        st.stop()
        
    st.info(f"Found {len(files)} files. Processing...")
    
    results = {}
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i, file_title in enumerate(files):
        status_text.text(f"Processing {i+1}/{len(files)}: {file_title}")
        
        wikitext, _ = fetch_wikitext(file_title, api_url=MEDIA_API_URL)
        caption = get_caption_from_text(wikitext)
        
        if not caption:
            results[file_title] = {"error": "No caption found"}
            progress_bar.progress((i + 1) / len(files))
            continue
            
        # 1. Blind Suggestions (with context)
        blind_suggestions = suggest_blind_categories(caption, context=category_input)
        
        # 2. Fuzzy Search (based on the blind suggestions)
        fuzzy_candidates = get_fuzzy_candidates(blind_suggestions, limit_per_suggestion=5)
        
        # 3. AI Filter (with context)
        final_picks = filter_fuzzy_categories(caption, fuzzy_candidates, context=category_input)
        
        results[file_title] = {
            "caption": caption,
            "1_blind_suggestions": blind_suggestions,
            "2_fuzzy_candidates": fuzzy_candidates,
            "3_final_picks": final_picks
        }
        
        progress_bar.progress((i + 1) / len(files))
        
    status_text.success("Processing complete!")
    
    # Save to JSON
    output_file = os.path.join(project_root, "auto_cat_results.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=4, ensure_ascii=False)
        
    st.success(f"✅ Results saved to `{output_file}` in your project root folder.")
    
    # Provide a download button for convenience
    with open(output_file, "rb") as file:
        st.download_button(
            label="Download JSON Results",
            data=file,
            file_name="auto_cat_results.json",
            mime="application/json"
        )
