import random
import os
import subprocess
import glob
from PIL import Image
import google.generativeai as genai
from src.config import GEMINI_API_KEY

genai.configure(api_key=GEMINI_API_KEY)

def extract_single_page(pdf_path, page_num, output_dir):
    """Extracts a single specific page from PDF to PNG."""
    # pdftoppm uses 1-based indexing for -f (first) and -l (last)
    prefix = os.path.join(output_dir, f"calibration_{page_num}")
    subprocess.run([
        "pdftoppm", "-png", "-f", str(page_num), "-l", str(page_num), 
        "-r", "150", # Low res is fine for page numbers, faster
        pdf_path, prefix
    ], check=True)
    
    # Return the generated file path
    files = glob.glob(f"{prefix}*.png")
    return files[0] if files else None

def get_printed_page_number(image_path):
    """Asks Gemini to find the page number."""
    model = genai.GenerativeModel('gemini-1.5-flash')
    img = Image.open(image_path)
    
    prompt = "Look at the header or footer of this page. Return ONLY the integer value of the printed page number. If there is no page number, return 'NONE'."
    
    try:
        response = model.generate_content([prompt, img])
        text = response.text.strip()
        if text.isdigit():
            return int(text)
    except:
        pass
    return None

def calculate_start_offset(pdf_path, total_pages):
    """
    Triangulates the 'Page 1' PDF index using random samples.
    Returns: The calculated PDF page number where 'Page 1' begins, or None if failed.
    """
    temp_dir = f".temp_calib_{os.path.basename(pdf_path)}"
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    try:
        # Pick 3 probe points: 20%, 50%, 80% through the book
        # We avoid the first/last 10% to skip Roman numerals and index pages
        probes = [
            int(total_pages * 0.2),
            int(total_pages * 0.5),
            int(total_pages * 0.8)
        ]
        
        offsets = []
        
        for pdf_page in probes:
            if pdf_page < 1: continue
            
            img_path = extract_single_page(pdf_path, pdf_page, temp_dir)
            if img_path:
                printed_num = get_printed_page_number(img_path)
                if printed_num:
                    # Logic: Offset = PDF_Page - Printed_Page
                    # If PDF Page 13 is Book Page 1, Offset is 12.
                    offset = pdf_page - printed_num
                    offsets.append(offset)
        
        # Cleanup temp images immediately
        import shutil
        shutil.rmtree(temp_dir)

        if not offsets:
            return None

        # CONSENSUS CHECK
        # We need at least 2 samples to agree to trust the automation
        from collections import Counter
        counts = Counter(offsets)
        most_common_offset, frequency = counts.most_common(1)[0]
        
        if frequency >= 2:
            # Found a reliable pattern!
            # The "First Numbered Page" is 1 + offset
            return 1 + most_common_offset
        else:
            # Data is too noisy (maybe unnumbered illustrations threw it off)
            return None

    except Exception as e:
        print(f"Calibration Error: {e}")
        return None
