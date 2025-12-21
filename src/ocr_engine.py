import os
import subprocess
import glob
import re
import shutil
from dataclasses import dataclass
from typing import List, Tuple, Optional, Callable
import pytesseract
from PIL import Image

@dataclass
class OcrConfig:
    has_cover_image: bool
    first_numbered_page_index: int  # The 1-based index where printed "Page 1" actually starts
    illustration_ranges: List[Tuple[int, int]]  # List of (start, end) ranges to skip numbering
    language: str = "eng"  # Tesseract language code (eng, deu, fas, etc.)

class OcrEngine:
    def __init__(self, file_path: str):
        """
        Initializes the engine for a specific PDF file.
        :param file_path: Absolute path to the PDF.
        """
        self.file_path = file_path
        self.work_dir = os.path.dirname(file_path)
        self.filename = os.path.basename(file_path)
        self.book_name = os.path.splitext(self.filename)[0]
        
        # We create a hidden temp folder next to the PDF to store PNGs
        self.cache_dir = os.path.join(self.work_dir, f".ocr_temp_{self.book_name}")
        
        # Output text file location (same folder as PDF)
        self.output_txt_path = os.path.join(self.work_dir, f"{self.book_name}.txt")

    def _to_roman(self, num: int) -> str:
        """
        Converts integer to lower-case roman numeral.
        Ported from your Node.js script.
        """
        if num <= 0: return ""
        romans = [
            ("M", 1000), ("CM", 900), ("D", 500), ("CD", 400),
            ("C", 100), ("XC", 90), ("L", 50), ("XL", 40),
            ("X", 10), ("IX", 9), ("V", 5), ("IV", 4), ("I", 1)
        ]
        result = ""
        for roman, value in romans:
            while num >= value:
                result += roman
                num -= value
        return result.lower()

    def _get_page_label(self, image_index: int, config: OcrConfig, illus_counter: int, real_page_counter: int) -> Tuple[str, int, int]:
        """
        Determines the label for the current page (Roman, Number, or illus.#).
        Returns: (Label String, Updated IllusCounter, Updated RealPageCounter)
        """
        # Check Illustration Ranges
        is_illustration = False
        for start, end in config.illustration_ranges:
            if start <= image_index <= end:
                is_illustration = True
                break

        if image_index < config.first_numbered_page_index:
            # Roman Numerals (Front Matter)
            # Offset by 1 if cover image exists
            roman_val = image_index - (1 if config.has_cover_image else 0)
            return self._to_roman(roman_val), illus_counter, real_page_counter
        
        elif is_illustration:
            # Illustrations
            label = f"illus.{illus_counter}"
            return label, illus_counter + 1, real_page_counter
        
        else:
            # Standard Numbering
            label = str(real_page_counter)
            return label, illus_counter, real_page_counter + 1

    def generate_images(self) -> int:
        """
        Runs pdftoppm to generate PNGs in the temp directory.
        Returns the count of images generated.
        """
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        
        # Output prefix for pdftoppm
        prefix = os.path.join(self.cache_dir, "page")
        
        # Check if we already have images (simple caching)
        existing_files = glob.glob(os.path.join(self.cache_dir, "*.png"))
        if len(existing_files) > 0:
            return len(existing_files)

        print(f"Generating images for {self.filename}...")
        
        # Using subprocess to call system pdftoppm
        # -r 300 sets DPI to 300 (good for OCR)
        cmd = ["pdftoppm", "-png", "-r", "300", self.file_path, prefix]
        subprocess.run(cmd, check=True)
        
        # Return count
        return len(glob.glob(os.path.join(self.cache_dir, "*.png")))

    def _natural_sort_key(self, s):
        """Helper to sort filenames like page-1.png, page-2.png, page-10.png correctly."""
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

    def run_ocr(self, config: OcrConfig, progress_callback: Optional[Callable[[int, int], None]] = None):
        """
        Main execution loop.
        1. Reads images.
        2. OCRs them.
        3. Formats template.
        4. Writes single .txt file.
        """
        # 1. Get all PNGs sorted naturally
        image_files = sorted(glob.glob(os.path.join(self.cache_dir, "*.png")), key=self._natural_sort_key)
        total_images = len(image_files)
        
        if total_images == 0:
            raise FileNotFoundError("No images found. Run generate_images() first.")

        # Counters
        illus_counter = 1
        real_page_counter = 1
        full_text_content = []

        print(f"Starting OCR for {self.book_name} ({config.language})...")

        for i, img_path in enumerate(image_files, start=1):
            # Update Progress Bar (if provided)
            if progress_callback:
                progress_callback(i, total_images)

            # Skip cover image if configured
            if config.has_cover_image and i == 1:
                print(f"Skipping cover image {i}")
                continue

            # A. Calculate Page Label
            label, illus_counter, real_page_counter = self._get_page_label(
                i, config, illus_counter, real_page_counter
            )

            # B. Perform OCR
            try:
                # Use PIL to open image, pass to pytesseract
                # Pass 'eng', 'deu', 'fas' etc directly
                text = pytesseract.image_to_string(Image.open(img_path), lang=config.language)
                
                # Clean generic garbage (form feed characters)
                text = text.replace('\f', '')
            except Exception as e:
                text = f"[OCR FAILED: {str(e)}]"

            # C. Format Template
            # {{page|label|file=Filename.pdf|page=index}}
            template = f"{{{{page|{label}|file={self.filename}|page={i}}}}}"
            
            # Combine
            page_content = f"{template}\n{text}\n"
            full_text_content.append(page_content)
            
            # Optional: Print status to console for debugging
            # print(f"Finished image {i} -> page {label}")

        # 4. Save to Disk
        with open(self.output_txt_path, "w", encoding="utf-8") as f:
            f.writelines(full_text_content)
        
        print(f"Success! Saved to {self.output_txt_path}")
        return self.output_txt_path

    def cleanup(self):
        """Removes the temporary PNG folder."""
        if os.path.exists(self.cache_dir):
            shutil.rmtree(self.cache_dir)
