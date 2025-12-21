import os
import subprocess
import glob
from collections import Counter
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv
import shutil

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
    """Asks Gemini to find the page number."""
    model = genai.GenerativeModel('gemini-3-flash-preview')
    try:
        with Image.open(image_path) as img:
            # UPDATED: Prompt is slightly more instructive
            prompt = "Look at the corners (header/footer) of this page image. Return ONLY the digit of the printed page number. If there is no page number, return 'NONE'."
            response = model.generate_content([prompt, img])
            raw_text = response.text.strip()
            
            # UPDATED: We return both the clean number AND the raw text for debugging
            clean_text = ''.join(filter(str.isdigit, raw_text))
            
            if clean_text and clean_text.isdigit():
                return int(clean_text), raw_text
            return None, raw_text
    except Exception as e:
        return None, str(e)

def calculate_start_offset(pdf_path, total_pages):
    """
    Triangulates the 'Page 1' PDF index.
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
    
    try:
        for pdf_page in probes:
            if pdf_page < 1: continue
            
            img_path = extract_single_page(pdf_path, pdf_page, temp_dir)
            
            if img_path:
                printed_num, raw_response = get_printed_page_number(img_path)
                
                if printed_num is not None:
                    offset = pdf_page - printed_num
                    offsets.append(offset)
                    print(f"      - PDF Pg {pdf_page} -> printed '{printed_num}' (Offset: {offset})")
                else:
                    # UPDATED: Print the RAW response so you know why it failed
                    print(f"      - PDF Pg {pdf_page} -> printed 'None' | Raw AI Response: '{raw_response}'")

        if not offsets:
            return None

        counts = Counter(offsets)
        most_common_offset, frequency = counts.most_common(1)[0]
        
        # Cleanup ONLY if successful. If failed, we keep files for inspection.
        if frequency >= 2:
            final_start = 1 + most_common_offset
            print(f"      > Consensus found! Offset {most_common_offset}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return final_start
        else:
             print(f"      > No consensus. Offsets found: {offsets}")
             print(f"      > DEBUG: Kept images in {temp_dir} for inspection.")
            
    except Exception as e:
        print(f"      Calibration crash: {e}")
            
    return None
