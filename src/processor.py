import fitz  # PyMuPDF
from PIL import Image
import io

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
