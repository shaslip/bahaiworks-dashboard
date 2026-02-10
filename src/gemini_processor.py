import os
import json
import re
import google.generativeai as genai
from pdf2image import convert_from_path
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# Configure Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# Your specific model
MODEL_NAME = 'gemini-3-flash-preview'

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
    """
    wikitext = ""
    for item in toc_list:
        title = item.get("title", "").strip()
        level = item.get("level", 1) # Default to 1 if missing
        
        if not title: continue
            
        if level == 1:
            line = f": [[/{title}|{title}]]"
        else:
            line = f":: {title}"
            
        wikitext += line + "\n"
    return wikitext

def extract_metadata_from_pdf(pdf_path, page_range_str):
    print(f"--- Debug: Extracting Metadata for {page_range_str} ---")
    pages_to_process = parse_range_string(page_range_str)
    images = []
    for p_num in pages_to_process:
        print(f"Debug: Converting page {p_num}...")
        img_list = convert_from_path(pdf_path, first_page=p_num, last_page=p_num)
        if img_list: images.append(img_list[0])

    if not images: return {"error": "No images extracted"}

    model = genai.GenerativeModel(MODEL_NAME)
    
    prompt = """
    Analyze these images of a book's copyright/title pages. 
    Output a single JSON object with exactly two keys:
    
    1. "copyright_text": A string containing the full, verbatim text from these pages (clean OCR).
    2. "data": A flat JSON object with these keys (leave blank if not found):
        - TITLE, FULL_TITLE, AUTHOR, EDITOR, TRANSLATOR, COMPILER
        - PUBLISHER, COUNTRY, PUBYEAR, PAGES, ISBN10, ISBN13
    
    Output strictly valid JSON.
    """
    
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    try:
        print("Debug: Sending Metadata request to Gemini...")
        response = model.generate_content([prompt, *images], safety_settings=safety_settings)
        print("Debug: Metadata response received.")
        
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {"error": "No JSON found in response", "raw": response.text}
    except Exception as e:
        print(f"Debug: Metadata Error: {e}")
        return {"error": f"API Error: {e}"}

def extract_toc_from_pdf(pdf_path, page_range_str):
    print(f"--- Debug: Extracting TOC for {page_range_str} ---")
    pages_to_process = parse_range_string(page_range_str)
    images = []
    
    try:
        for p_num in pages_to_process:
            print(f"Debug: Converting page {p_num}...")
            img_list = convert_from_path(pdf_path, first_page=p_num, last_page=p_num)
            if img_list: images.append(img_list[0])
    except Exception as e:
        print(f"Debug: PDF Conversion Error: {e}")
        return {"toc_json": [], "toc_wikitext": "", "error": f"PDF Conversion Error: {e}"}

    model = genai.GenerativeModel(MODEL_NAME)
    
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
    
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }

    try:
        print("Debug: Sending TOC request to Gemini...")
        response = model.generate_content([prompt, *images], safety_settings=safety_settings)
        print("Debug: TOC response received.")
        
        if response.prompt_feedback:
             print(f"Debug: Prompt Feedback: {response.prompt_feedback}")
        
        match = re.search(r'\[.*\]', response.text, re.DOTALL)
        
        if match:
            toc_list = json.loads(match.group(0))
            toc_wikitext = json_to_wikitext(toc_list)
            
            return {
                "toc_json": toc_list,
                "toc_wikitext": toc_wikitext
            }
        else:
            print(f"Debug: No JSON found. Raw text: {response.text}")
            return {"toc_json": [], "toc_wikitext": "", "error": "No JSON List found", "raw": response.text}
            
    except Exception as e:
        print(f"Debug: API Exception: {e}")
        return {"toc_json": [], "toc_wikitext": "", "error": str(e)}

def proofread_page(image):
    """
    Strict archival transcription (Original Logic).
    """
    model = genai.GenerativeModel(MODEL_NAME)
    prompt = """
    You are a strict archival transcription engine. 
    1. Transcribe the text from this page image character-for-character.
    2. Do NOT correct grammar or modernization spelling.
    3. If the text has an OBVIOUS typo (e.g. "sentance"), transcribe it as: {{sic|sentance|sentence}}
    4. Preserve paragraph breaks.
    5. Return ONLY the text. No markdown formatting blocks (```), no conversational filler.
    """
    try:
        response = model.generate_content([prompt, image])
        return response.text.strip()
    except Exception as e:
        return f"Error: {e}"

def proofread_with_formatting(image):
    """
    Transcription WITH MediaWiki formatting (Headers, Tables, Bold/Italic).
    """
    model = genai.GenerativeModel(MODEL_NAME)
    
    prompt = """
    You are an expert transcriber and editor for a MediaWiki archive.
    
    Your task:
    1.  Transcribe the text from this image exactly as it appears, correcting only obvious OCR errors.
    2.  **FORMATTING IS CRITICAL:**
        -   If you see a **Header**, use MediaWiki syntax (e.g., `== Header ==` or `=== Subheader ===`).
        -   If you see a **Table**, transcribe it as a MediaWiki table (`{| class="wikitable" ... |}`).
        -   If you see **Bold** or *Italic* text, preserve it using `'''bold'''` and `''italic''`.
        -   Do NOT add any conversational filler ("Here is the text").
        -   Output ONLY the raw wikitext.
    """
    
    try:
        response = model.generate_content([prompt, image])
        return response.text.strip()
    except Exception as e:
        return f"GEMINI_ERROR: {str(e)}"
