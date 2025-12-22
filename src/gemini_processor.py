import os
import json
import google.generativeai as genai
from pdf2image import convert_from_path

# Configure Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

def parse_range_string(range_str):
    """Parses '1-3, 5' into [1, 2, 3, 5]"""
    pages = []
    if not range_str: return pages
    parts = range_str.split(',')
    for part in parts:
        part = part.strip()
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.extend(range(start, end + 1))
        elif part.isdigit():
            pages.append(int(part))
    return sorted(list(set(pages)))

def extract_metadata_from_pdf(pdf_path, page_range_str):
    """
    1. Converts PDF pages to images.
    2. Sends images to Gemini.
    3. Returns a JSON dictionary matching the Wikibase importer keys.
    """
    pages_to_process = parse_range_string(page_range_str)
    
    # Extract images (pdf2image uses 1-based indexing, same as humans)
    # We grab only the requested pages to save memory/bandwidth
    images = []
    # Note: convert_from_path might be slow for huge PDFs if not optimized, 
    # but efficient enough for grabbing pages 1-5.
    # We loop to grab specific pages because they might be non-contiguous
    for p_num in pages_to_process:
        # first_page and last_page are inclusive 1-based indices
        img_list = convert_from_path(pdf_path, first_page=p_num, last_page=p_num)
        if img_list:
            images.append(img_list[0])

    if not images:
        return {"error": "No images extracted"}

    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    prompt = """
    Analyze these images of a book's copyright/title pages.
    Extract the following metadata into a pure JSON object. 
    Do not use Markdown formatting (no ```json). 
    
    Keys required:
    - TITLE (The short title)
    - FULL_TITLE (Subtitle included)
    - AUTHOR (Comma separated names)
    - EDITOR (Comma separated names)
    - TRANSLATOR (Comma separated names)
    - COMPILER (Comma separated names)
    - PUBLISHER
    - COUNTRY (Country of publication)
    - PUBYEAR (Year only, e.g. 1995)
    - PAGES (Total pages if listed, else null)
    - ISBN10
    - ISBN13
    
    If a field is not found, return an empty string "".
    """
    
    response = model.generate_content([prompt, *images])
    
    try:
        # Clean potential markdown just in case
        clean_text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean_text)
    except Exception as e:
        return {"error": f"Failed to parse JSON: {e}", "raw": response.text}

def extract_toc_from_pdf(pdf_path, page_range_str):
    """
    Extracts TOC as MediaWiki Markup.
    """
    pages_to_process = parse_range_string(page_range_str)
    images = []
    for p_num in pages_to_process:
        img_list = convert_from_path(pdf_path, first_page=p_num, last_page=p_num)
        if img_list:
            images.append(img_list[0])
            
    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    prompt = """
    Extract the Table of Contents from these images.
    Return the output ONLY as MediaWiki markup list format.
    
    Rules:
    1. Use * for chapters.
    2. Use ** for sub-chapters.
    3. Format links as: * [[/Chapter Name|Chapter Name]] .. Page X
    4. Do not include headers like "Contents" or "Index".
    5. Correct any OCR errors in page numbers.
    """
    
    response = model.generate_content([prompt, *images])
    return response.text
