import fitz  # PyMuPDF
from PIL import Image
import io
import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
if api_key:
    genai.configure(api_key=api_key)

def extract_preview_images(file_path: str, max_pages: int = 3):
    """
    Opens a PDF and converts the first few pages into PIL Images.
    Returns a list of PIL Image objects.
    """
    images = []
    try:
        doc = fitz.open(file_path)
        # Determine how many pages to scan (min of available vs max_requested)
        count = min(doc.page_count, max_pages)
        
        for i in range(count):
            page = doc.load_page(i)
            # Render page to an image (pixmap) at standard resolution (72 dpi is usually enough for OCR)
            # Zooming 2x (matrix=fitz.Matrix(2, 2)) improves OCR accuracy for old fonts
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            
            # Convert to PIL Image
            img_data = pix.tobytes("png")
            image = Image.open(io.BytesIO(img_data))
            images.append(image)
            
        doc.close()
        return images
    
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return []

def merge_pdf_pair(cover_path: str, content_path: str, output_path: str) -> bool:
    """
    Merges two PDFs: Cover + Content into a single file at output_path.
    """
    try:
        if not os.path.exists(cover_path) or not os.path.exists(content_path):
            print(f"    ❌ Merge failed: One or more input files missing.")
            return False

        doc_master = fitz.open(cover_path)
        doc_content = fitz.open(content_path)
        
        # Append content to the end of master (cover)
        doc_master.insert_pdf(doc_content)
        
        doc_master.save(output_path)
        doc_master.close()
        doc_content.close()
        return True
    except Exception as e:
        print(f"    ❌ Merge Error: {e}")
        return False

def _is_page_double(doc, page_num, model):
    """Helper: returns True if page is a double spread."""
    try:
        page = doc.load_page(page_num)
        pix = page.get_pixmap(matrix=fitz.Matrix(0.5, 0.5)) # Low res is fine
        img_data = pix.tobytes("png")
        img = Image.open(io.BytesIO(img_data))
        
        prompt = "Is this image a 'double-page spread' (two physical book pages on one scan)? Reply ONLY with 'YES' or 'NO'."
        response = model.generate_content([prompt, img])
        return response.text.strip().upper() == "YES"
    except:
        return False

def analyze_split_boundaries(pdf_path):
    """
    Scans first 4 and last 4 pages to find where double-pages begin and end.
    Returns (start_index, end_index)
    """
    doc = fitz.open(pdf_path)
    total = len(doc)
    
    head_range = range(0, min(4, total))
    tail_range = range(max(0, total - 4), total)
    
    first_double = 0
    last_double = total - 1
    
    # Use same model as auto_config
    model = genai.GenerativeModel('gemini-3-flash-preview')
    
    # Scan Head
    for i in head_range:
        if _is_page_double(doc, i, model):
            first_double = i
            break
            
    # Scan Tail (Backward)
    for i in reversed(tail_range):
        if _is_page_double(doc, i, model):
            last_double = i
            break
            
    doc.close()
    return first_double, last_double

def split_pdf_doubles(input_path, output_path, start_idx, end_idx):
    """
    Splits pages in range [start_idx, end_idx] into two.
    """
    try:
        src = fitz.open(input_path)
        out = fitz.open()
        
        for i in range(len(src)):
            if start_idx <= i <= end_idx:
                # SPLIT
                page = src[i]
                r = page.rect
                
                # Left
                l_page = out.new_page(width=r.width/2, height=r.height)
                l_page.show_pdf_page(l_page.rect, src, i, clip=fitz.Rect(0, 0, r.width/2, r.height))
                
                # Right
                r_page = out.new_page(width=r.width/2, height=r.height)
                r_page.show_pdf_page(r_page.rect, src, i, clip=fitz.Rect(r.width/2, 0, r.width, r.height))
            else:
                # KEEP
                out.insert_pdf(src, from_page=i, to_page=i)
                
        out.save(output_path)
        src.close()
        out.close()
        return True
    except Exception as e:
        print(f"    ❌ Split Error: {e}")
        return False
