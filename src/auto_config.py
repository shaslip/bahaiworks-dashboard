import os
import subprocess
import glob
from collections import Counter
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv
import shutil
import re

Image.MAX_IMAGE_PIXELS = None

# Load env variables directly
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    try:
        from src.config import GEMINI_API_KEY as api_key
    except ImportError:
        raise ValueError("GEMINI_API_KEY not found.")

genai.configure(api_key=api_key)

def extract_single_page(pdf_path, page_num, output_dir):
    """Extracts a single specific page from PDF to PNG."""
    prefix = os.path.join(output_dir, f"calibration_{page_num}")
    
    existing = glob.glob(f"{prefix}*.png")
    if existing: return existing[0]

    try:
        # UPDATED: Increased DPI to 300 for better OCR on old scans
        subprocess.run([
            "pdftoppm", "-png", "-f", str(page_num), "-l", str(page_num), 
            "-r", "300", 
            pdf_path, prefix
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        files = glob.glob(f"{prefix}*.png")
        return files[0] if files else None
    except Exception as e:
        print(f"      [Error extracting page {page_num}]: {e}")
        return None

def get_printed_page_number(image_path):
    """Asks Gemini for page number AND double-page detection."""
    # Using the model string from your file
    model = genai.GenerativeModel('gemini-3-flash-preview') 
    
    try:
        with Image.open(image_path) as img:
            prompt = (
                "Two tasks:\n"
                "1. Look at the corners. Return the printed page number digit. If none, return 'NONE'.\n"
                "2. Is this image a 'double-page spread' (scanned with two physical book pages visible on one PDF page)?\n"
                "Reply in this format: {PageNumber}|{YES/NO}"
            )
            response = model.generate_content([prompt, img])
            raw_text = response.text.strip()
            
            # Parse: "45|YES" or "NONE|NO"
            parts = raw_text.split('|')
            
            # Extract Number
            clean_text = ''.join(filter(str.isdigit, parts[0]))
            page_num = int(clean_text) if clean_text.isdigit() else None
            
            # Extract Double Page Status
            is_double = False
            if len(parts) > 1:
                is_double = parts[1].strip().upper() == "YES"

            return page_num, raw_text, is_double

    except Exception as e:
        return None, str(e), False

def calculate_start_offset(pdf_path, total_pages):
    """
    Triangulates 'Page 1' index and detects double-page spreads.
    Returns: (offset, is_double_page_bool)
    """
    temp_dir = f".temp_calib_{os.path.basename(pdf_path)}"
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    probes = [
        int(total_pages * 0.2),
        int(total_pages * 0.5),
        int(total_pages * 0.8)
    ]
    
    print(f"      Probing PDF pages: {probes}")
    
    offsets = []
    double_page_votes = 0
    valid_samples = 0
    
    try:
        for pdf_page in probes:
            if pdf_page < 1: continue
            
            img_path = extract_single_page(pdf_path, pdf_page, temp_dir)
            
            if img_path:
                printed_num, raw_response, is_double = get_printed_page_number(img_path)
                valid_samples += 1
                
                if is_double:
                    double_page_votes += 1

                if printed_num is not None:
                    offset = pdf_page - printed_num
                    offsets.append(offset)
                    print(f"      - PDF Pg {pdf_page} -> printed '{printed_num}' (Offset: {offset}) [Double: {is_double}]")
                else:
                    print(f"      - PDF Pg {pdf_page} -> printed 'None' | Raw: '{raw_response}'")

        # Determine Double Page Consensus (>50%)
        is_double_page_detected = False
        if valid_samples > 0:
            is_double_page_detected = double_page_votes >= (valid_samples / 2)

        if not offsets:
            return None, is_double_page_detected

        counts = Counter(offsets)
        most_common_offset, frequency = counts.most_common(1)[0]
        
        if frequency >= 2:
            final_start = 1 + most_common_offset
            print(f"      > Consensus found! Offset {most_common_offset}. Double Page: {is_double_page_detected}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return final_start, is_double_page_detected
        else:
             print(f"      > No consensus. Offsets found: {offsets}")
             print(f"      > DEBUG: Kept images in {temp_dir} for inspection.")
             # CHANGED: Return the detected status even if offsets failed
             return None, is_double_page_detected
            
    except Exception as e:
        print(f"      Calibration crash: {e}")
            
    # CHANGED: Fallback return
    return None, is_double_page_detected
