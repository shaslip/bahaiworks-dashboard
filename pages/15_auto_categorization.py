import streamlit as st
import os
import sys
import re
import requests

# --- Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

from src.mediawiki_uploader import fetch_wikitext, get_image_url
from src.gemini_processor import suggest_blind_categories, filter_fuzzy_categories
from src.category_manager import get_fuzzy_candidates

MEDIA_API_URL = 'https://bahai.media/api.php'

st.set_page_config(page_title="Auto-Categorization Test", page_icon="🗂️", layout="wide")

def get_caption_from_text(content):
    if not content: return ""
    match = re.search(r'\|\s*caption\s*=\s*(.*?)\n\|', content, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""

st.title("🗂️ Auto-Categorization Sandbox")
st.markdown("Test the fuzzy logic and AI filtering on a single image before building the batch queue.")

st.sidebar.header("Configuration")
st.sidebar.info("Edit `category_exclusions.txt` and `category_aliases.txt` in your project root to tune the fuzzy search.")

file_input = st.text_input("Enter a File Name", placeholder="e.g. File:Race_Unity_Day_in_Austin_Texas.png")

if st.button("🧪 Run Test", type="primary"):
    if not file_input:
        st.warning("Please enter a file name.")
        st.stop()
        
    if not file_input.lower().startswith("file:"):
        file_input = "File:" + file_input

    with st.spinner("Fetching data..."):
        # 1. Fetch text and image
        wikitext, _ = fetch_wikitext(file_input, api_url=MEDIA_API_URL)
        image_url = get_image_url(file_input, api_url=MEDIA_API_URL)
        
        if not wikitext:
            st.error("Could not fetch wikitext. Does the file exist?")
            st.stop()
            
        caption = get_caption_from_text(wikitext)
        
        if not caption:
            st.warning("No caption found in the file's {{cs}} template. AI needs a caption to work.")
            st.stop()

    col1, col2 = st.columns([1, 1.5])
    
    with col1:
        st.subheader("Source Image")
        if image_url:
            st.image(image_url, use_container_width=True)
        st.info(f"**Caption:** {caption}")
        
    with col2:
        st.subheader("AI Analysis")
        
        # 2. Blind Suggestions
        with st.spinner("Getting blind suggestions from Gemini..."):
            blind_suggestions = suggest_blind_categories(caption)
            
        st.markdown("### 1. Blind AI Suggestions")
        st.caption("Categories Gemini *wants* to use (Good for finding new aliases to add to category_aliases.txt)")
        if blind_suggestions:
            for cat in blind_suggestions:
                st.write(f"- {cat}")
        else:
            st.write("*None*")
            
        # 3. Fuzzy Search
        with st.spinner("Running local fuzzy search..."):
            fuzzy_candidates = get_fuzzy_candidates(caption, limit=20)
            
        with st.expander("🔍 View Raw Top 20 Fuzzy Matches"):
            st.write(fuzzy_candidates)
            
        # 4. AI Filter
        with st.spinner("Filtering fuzzy matches with Gemini..."):
            final_picks = filter_fuzzy_categories(caption, fuzzy_candidates)
            
        st.markdown("### 2. Final AI Picks")
        st.caption("Categories Gemini selected from the Top 20 fuzzy matches")
        if final_picks:
            for cat in final_picks:
                st.success(f"✅ [[Category:{cat}]]")
        else:
            st.warning("Gemini rejected all fuzzy matches.")
            
    st.divider()
    st.write("If the **Final AI Picks** are missing something that the **Blind AI Suggestions** caught, add an entry to `category_aliases.txt` and run the test again!")
