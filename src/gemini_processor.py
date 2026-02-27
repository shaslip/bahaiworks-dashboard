import os
import json
import re
import time
import io
import json_repair
import google.generativeai as genai
from google.cloud import documentai
from google.api_core.client_options import ClientOptions
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

def transcribe_with_document_ai(image):
    """
    Fallback function using Google Cloud Document AI (OCR).
    Used when Gemini refuses to process content due to copyright/recitation.
    """
    project_id = os.environ.get("GCP_PROJECT_ID")
    location = os.environ.get("GCP_LOCATION", "us")
    processor_id = os.environ.get("GCP_PROCESSOR_ID")

    if not all([project_id, location, processor_id]):
        return "DOCAI_ERROR: Missing GCP_PROJECT_ID, GCP_LOCATION, or GCP_PROCESSOR_ID in .env"

    try:
        # You must set the api_endpoint if you use a location other than 'us'.
        opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
        client = documentai.DocumentProcessorServiceClient(client_options=opts)
        
        name = client.processor_path(project_id, location, processor_id)

        # Convert PIL Image to Bytes
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format='JPEG')
        content = img_byte_arr.getvalue()

        # Load Binary Data into Document AI RawDocument Object
        raw_document = documentai.RawDocument(content=content, mime_type="image/jpeg")

        # Configure the process request
        request = documentai.ProcessRequest(name=name, raw_document=raw_document)

        # Process the document
        result = client.process_document(request=request, timeout=120)
        
        # Return the raw text
        return result.document.text

    except Exception as e:
        return f"DOCAI_ERROR: {str(e)}"

def reformat_raw_text(raw_text):
    """
    Step 2 of Fallback: Use Gemini to format the raw OCR text.
    This bypasses the image-based copyright filter by processing text-to-text.
    """
    # Fast-fail: If DocAI returned nothing, it's a blank page. Save the API call.
    if not raw_text or not raw_text.strip():
        return "--BLANK--"

    model = genai.GenerativeModel(MODEL_NAME)
    
    prompt = """
    You are a MediaWiki formatting engine. 
    I will provide raw OCR text. Your job is to format it for a Baha'i archive.
    
    RULES:
    1.  **Do NOT rewrite content.** Only format it.
    2.  **Remove** page headers, running heads, and page numbers.
    3.  **ORTHOGRAPHY:** You MUST match the curly apostrophe (’) for Baha'i terms:
        - "Bahá’í", "Bahá’u’lláh", "‘Abdu’l-Bahá" (match case of origional document)
    4.  **FORMATTING:**
        - Use `== Header ==` for section headers.
    5.  Preserve paragraph breaks.
    6.  **BLANK PAGES:** If the text provided contains no meaningful content, or if after removing headers/page numbers there is no main content left, return ONLY the string: --BLANK--
    7.  **NO CONVERSATION:** Output ONLY the formatted wikitext or --BLANK--. Do not include any conversational filler like "Here is the text" or "I am ready".
    
    RAW TEXT START:
    """
    
    try:
        # We append the text to the prompt
        full_prompt = prompt + f"\n{raw_text}\nRAW TEXT END"
        
        response = model.generate_content(
            full_prompt,
            request_options={"timeout": 120}
        )
        
        # If Gemini *still* refuses (unlikely but possible on text), return raw text as fail-safe
        if response.prompt_feedback.block_reason:
             return f"FORMATTING_ERROR: {raw_text}" # Return raw so we at least save something
             
        return response.text.strip()
        
    except Exception as e:
        return f"FORMATTING_ERROR: {raw_text}"

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
        response = model.generate_content(
            [prompt, image],
            request_options={"timeout": 120}
        )
        return response.text.strip()
    except Exception as e:
        return f"Error: {e}"

def proofread_with_formatting(image):
    """
    Transcription WITH MediaWiki formatting.
    - Enforces specific Baha'i orthography (curly apostrophes).
    - Removes page headers/footers/page numbers.
    - Includes RETRY logic for Copyright/Recitation errors.
    """
    model = genai.GenerativeModel(MODEL_NAME)
    
    # Prompt updated to emphasize "Text Extraction" to potentially bypass recitation triggers
    prompt = """
    You are an expert transcriber and editor for a MediaWiki archive.
    
    Your task:
    1.  **CHECK FOR CONTENT:** If the page is blank, illegible, contains only faint bleed-through, or is just a blank lined page, return ONLY the string: --BLANK--
    2.  Extract the **MAIN CONTENT** of this page.
    3.  From the second page on, **EXCLUDE** all page headers, running heads, and page numbers.
    4.  **ORTHOGRAPHY:** You MUST match the curly apostrophe (’) if used in the document, eg:
        -   Write "Bahá’í" (Not Bahá'í)
        -   Write "Bahá’u’lláh" (Not Bahá'u'lláh)
        -   Write "‘Abdu’l-Bahá" (Not 'Abdu'l-Bahá)
    5.  **FORMATTING:**
        -   If you see a **Header** (that is part of the text, not a running head), use `== Header ==`.
        -   If you see a **Table**, use `{| class="wikitable" ... |}`.
        -   If you see **Bold** or *Italic*, use `'''bold'''` and `''italic''`.
        -   For other cases, use standard MediaWiki formatting where appropriate.
        -   If the text has an OBVIOUS typo (e.g. "sentance"), transcribe it as: {{sic|sentance|sentence}}
        -   Note: <poem> tags are not supported.
    6.  Paragraph breaks require an extra return.
    7.  Output ONLY the clean wikitext.
    """
    
    max_retries = 1
    
    for attempt in range(max_retries + 1):
        try:
            # Generate content
            response = model.generate_content(
                [prompt, image],
                request_options={"timeout": 120}
            )
            
            # Check for Recitation/Copyright block (finish_reason 4)
            # We check this BEFORE accessing .text to avoid the crash
            if response.candidates and response.candidates[0].finish_reason == 4:
                print(f"Blocked by Copyright/Recitation filters.")
                return "GEMINI_ERROR: Recitation/Copyright Block"

            # If we get here, it should be safe to access .text
            text = response.text.strip()
            
            # Remove leading whitespace from every line.
            text = re.sub(r'^[ \t]+', '', text, flags=re.MULTILINE)
            
            return text

        except Exception as e:
            print(f"Attempt {attempt + 1} Error: {str(e)}")
            if attempt < max_retries:
                time.sleep(15)
            else:
                return f"GEMINI_ERROR: {str(e)}"

def get_chapter_split_indices(page_text, target_chapter, unmapped_chapters, custom_instruction):
    """
    Chunks page text into paragraphs and asks Gemini to identify the starting index for specific chapters.
    """
    model = genai.GenerativeModel(MODEL_NAME)
    
    # Chunk the page text by natural paragraph breaks
    blocks = [b.strip() for b in re.split(r'\n{2,}', page_text) if b.strip()]
    blocks_dict = {str(i): block for i, block in enumerate(blocks)}
    blocks_json = json.dumps(blocks_dict, indent=2)
    
    chapters_to_find = [target_chapter] + unmapped_chapters
    
    prompt = f"""
    You are a text processing assistant. I have a single page of text that has been chunked into numbered blocks.
    
    Context: {custom_instruction}
    
    Your task is to identify which block index marks the START of the following chapters/sections:
    {json.dumps(chapters_to_find)}
    
    Here are the text blocks:
    {blocks_json}
    
    Return ONLY a valid JSON object where the keys are the chapter names found, and the values are the integer block index where that chapter starts. 
    If a chapter is not found on this page, do not include it in the JSON.
    Do not output any markdown formatting or backticks, just the raw JSON.
    Example: {{"{target_chapter}": 4, "Another Chapter": 7}}
    """
    
    try:
        response = model.generate_content(prompt)
        result = json_repair.loads(response.text)
        return result, blocks
    except Exception as e:
        print(f"LLM split identification failed: {e}")
        return {}, blocks

def apply_chunked_split(page_text, target_chapter, unmapped_chapters, custom_instruction):
    """
    Divides a single page of text into multiple chapter chunks based on LLM-identified indices.
    """
    indices, blocks = get_chapter_split_indices(page_text, target_chapter, unmapped_chapters, custom_instruction)
    
    if not indices or target_chapter not in indices:
        return {"_previous_": page_text} # Fallback if LLM fails
        
    # Sort the found chapters by their starting block index ascending
    sorted_splits = sorted(indices.items(), key=lambda x: int(x[1]))
    results = {}
    
    # Everything before the very first split belongs to the previous chapter/page
    first_split_idx = int(sorted_splits[0][1])
    results["_previous_"] = "\n\n".join(blocks[:first_split_idx])
    
    # Extract text for each identified chapter based on the boundaries
    for i, (chap_name, start_idx) in enumerate(sorted_splits):
        start_idx = int(start_idx)
        end_idx = int(sorted_splits[i+1][1]) if i + 1 < len(sorted_splits) else len(blocks)
        results[chap_name] = "\n\n".join(blocks[start_idx:end_idx])
        
    return results
