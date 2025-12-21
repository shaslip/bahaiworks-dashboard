import os
import subprocess
import glob
from collections import Counter
from PIL import Image
import google.generativeai as genai
from dotenv import load_dotenv

# Load env variables directly to ensure key is found
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    # Fallback to config file if .env fails
    try:
        from src.config import GEMINI_API_KEY as api_key
    except ImportError:
        raise ValueError("GEMINI_API_KEY not found in environment or config.")

genai.configure(api_key=api_key)

def extract_single_page(pdf_path, page_num, output_dir):
    """Extracts a single specific page from PDF to PNG."""
    prefix = os.path.join(output_dir, f"calibration_{page_num}")
    
    # Check if we already have it (unlikely but good practice)
    existing = glob.glob(f"{prefix}*.png")
    if existing: return existing[0]

    # pdftoppm uses 1-based indexing
    try:
        subprocess.run([
            "pdftoppm", "-png", "-f", str(page_num), "-l", str(page_num), 
            "-r", "150", # 150 DPI is plenty for just reading a page number
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
        img = Image.open(image_path)
        prompt = "Return ONLY the integer value of the printed page number in the header/footer. If none, return 'NONE'."
        
        response = model.generate_content([prompt, img])
        text = response.text.strip()
        
        # Clean response (sometimes AI says "Page 5")
        text = ''.join(filter(str.isdigit, text))
        
        if text and text.isdigit():
            return int(text)
    except Exception:
        pass # Fail silently
    return None

def calculate_start_offset(pdf_path, total_pages):
    """
    Triangulates the 'Page 1' PDF index.
    Rule: We need at least 2 samples to result in the exact same offset.
    """
    temp_dir = f".temp_calib_{os.path.basename(pdf_path)}"
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)

    # We probe 20%, 50%, and 80% marks
    probes = [
        int(total_pages * 0.2),
        int(total_pages * 0.5),
        int(total_pages * 0.8)
    ]
    
    offsets = []
    
    try:
        for pdf_page in probes:
            if pdf_page < 1: continue
            
            # 1. Extract
            img_path = extract_single_page(pdf_path, pdf_page, temp_dir)
            
            # 2. Analyze
            if img_path:
                printed_num = get_printed_page_number(img_path)
                
                # 3. Calculate Offset
                if printed_num is not None:
                    # Offset calculation: 
                    # If PDF Page 50 is Book Page 38, offset is 12.
                    # This means Book Page 1 is at PDF Page (1 + 12) = 13.
                    offset = pdf_page - printed_num
                    offsets.append(offset)

        # 4. The "2 out of 3" Consensus
        if not offsets:
            return None

        counts = Counter(offsets)
        most_common_offset, frequency = counts.most_common(1)[0]
        
        # We accept if at least 2 samples agree (could be 2/3 or 2/2 if one failed)
        if frequency >= 2:
            # Return the PDF page number where Book Page 1 starts
            return 1 + most_common_offset
            
    except Exception as e:
        print(f"Calibration crash: {e}")
        
    finally:
        # Always cleanup temp files
        import shutil
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            
    return None
