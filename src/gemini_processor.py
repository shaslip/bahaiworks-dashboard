import os
import json
import re
import google.generativeai as genai
from pdf2image import convert_from_path

# Configure Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

def parse_range_string(range_str):
    pages = []
    if not range_str: return pages
    parts = range_str.split(',')
    for part in parts:
        part = part.strip()
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
                pages.extend(range(start, end + 1))
            except: pass
        elif part.isdigit():
            pages.append(int(part))
    return sorted(list(set(pages)))

def json_to_wikitext(toc_list):
    """
    Converts the structured JSON list into MediaWiki format.
    Format: 
    - Level 1 (Chapters): : [[/Title|Title]]
    - Level 2+ (Subtopics): :: Title (No link)
    """
    wikitext = ""
    for item in toc_list:
        title = item.get("title", "").strip()
        level = item.get("level", 1) # Default to 1 if missing
        
        # Skip empty entries
        if not title: continue
            
        if level == 1:
            # Main Chapter: Link it
            line = f": [[/{title}|{title}]]"
        else:
            # Subtopic: Indent using :: and do not link
            line = f":: {title}"
            
        wikitext += line + "\n"
    return wikitext

def extract_metadata_from_pdf(pdf_path, page_range_str):
    pages_to_process = parse_range_string(page_range_str)
    images = []
    for p_num in pages_to_process:
        img_list = convert_from_path(pdf_path, first_page=p_num, last_page=p_num)
        if img_list: images.append(img_list[0])

    if not images: return {"error": "No images extracted"}

    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    prompt = """
    Analyze these images of a book's copyright/title pages. 
    Output a single JSON object with exactly two keys:
    
    1. "copyright_text": A string containing the full, verbatim text from these pages (clean OCR).
    2. "data": A flat JSON object with these keys (leave blank if not found):
       - TITLE, FULL_TITLE, AUTHOR, EDITOR, TRANSLATOR, COMPILER
       - PUBLISHER, COUNTRY, PUBYEAR, PAGES, ISBN10, ISBN13
    
    Output strictly valid JSON.
    """
    
    try:
        response = model.generate_content([prompt, *images])
        # Robust regex extraction to ignore markdown code blocks
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {"error": "No JSON found in response", "raw": response.text}
    except Exception as e:
        return {"error": f"API Error: {e}"}

def extract_toc_from_pdf(pdf_path, page_range_str):
    """
    Returns a dict with 'toc_json' (list) and 'toc_wikitext' (string).
    """
    pages_to_process = parse_range_string(page_range_str)
    images = []
    for p_num in pages_to_process:
        img_list = convert_from_path(pdf_path, first_page=p_num, last_page=p_num)
        if img_list: images.append(img_list[0])
            
    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    prompt = """
    Analyze these images of a Table of Contents.
    Output a single JSON List of Objects (Array). 
    Do NOT output a dictionary, just the list [ ... ].
    
    Each object must have:
    - "title": The chapter title string.
    - "author": A list of strings (["Name"]) or [] if none.
    - "page_range": String (e.g., "5-10" or just "5"). Infer the end page if possible.
    - "level": Integer. 1 for main chapters, 2 for indented sub-topics/sections.
    
    IMPORTANT: Look closely at visual indentation. 
    - Bold, larger, or left-aligned text is usually Level 1.
    - Indented text or smaller text under a main header is Level 2.
    
    Output strictly valid JSON.
    """
    
    try:
        response = model.generate_content([prompt, *images])
        
        # Regex to find the list [ ... ]
        match = re.search(r'\[.*\]', response.text, re.DOTALL)
        
        if match:
            toc_list = json.loads(match.group(0))
            # Python generates the wikitext to avoid JSON syntax errors
            toc_wikitext = json_to_wikitext(toc_list)
            
            return {
                "toc_json": toc_list,
                "toc_wikitext": toc_wikitext
            }
        else:
            return {"toc_json": [], "toc_wikitext": "", "error": "No JSON List found", "raw": response.text}
            
    except Exception as e:
        return {"toc_json": [], "toc_wikitext": "", "error": str(e)}
