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
        subprocess.run([
            "pdftoppm", "-png", "-f", str(page_num), "-l", str(page_num), 
            "-r", "150", 
            pdf_path, prefix
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        files = glob.glob(f"{prefix}*.png")
        return files[0] if files else None
    except Exception as e:
        print(f"   [Error extracting page {page_num}]: {e}")
        return None

def get_printed_page_number(image_path):
    """Asks Gemini to find the page number."""
    model = genai.GenerativeModel('gemini-1.5-flash')
    try:
        # FIX 1: Use 'with' context manager to ensure file closes immediately
        with Image.open(image_path) as img:
            prompt = "Return ONLY the integer value of the printed page number in the header/footer. If none, return 'NONE'."
            response = model.generate_content([prompt, img])
            text = response.text.strip()
            
            # Clean response
            text = ''.join(filter(str.isdigit, text))
            
            if text and text.isdigit():
                return int(text)
    except Exception:
        pass 
    return None

def calculate_start_offset(pdf_path, total_pages):
    """
    Triangulates the 'Page 1' PDF index.
    """
    temp_dir = f".temp_calib_{os.path.basename(pdf_path)}"
    # Ensure directory exists
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    probes = [
        int(total_pages * 0.2),
        int(total_pages * 0.5),
        int(total_pages * 0.8)
    ]
    
    offsets = []
    
    try:
        for pdf_page in probes:
            if pdf_page < 1: continue
            
            img_path = extract_single_page(pdf_path, pdf_page, temp_dir)
            
            if img_path:
                printed_num = get_printed_page_number(img_path)
                
                if printed_num is not None:
                    offset = pdf_page - printed_num
                    offsets.append(offset)

        if not offsets:
            return None

        counts = Counter(offsets)
        most_common_offset, frequency = counts.most_common(1)[0]
        
        if frequency >= 2:
            return 1 + most_common_offset
            
    except Exception as e:
        print(f"Calibration crash: {e}")
        
    finally:
        # FIX 2: Add ignore_errors=True so a stuck file doesn't crash the factory
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
            
    return None
