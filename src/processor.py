import fitz  # PyMuPDF
from PIL import Image
import io
import os

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
