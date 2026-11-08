import os
import subprocess
import glob
import re
import shutil
import concurrent.futures
import fitz
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

        if image_index <= config.first_numbered_page_index:
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

    def generate_images(self, chunk_size: int = 20) -> int:
        """
        Runs pdftoppm to generate PNGs in parallel chunks.
        Reuses existing PNGs if all pages are already rendered.
        """
        # 1. Get total pages in the PDF first
        with fitz.open(self.file_path) as pdf:
            total_pages = len(pdf)

        # 2. Check if all PNGs already exist and are valid (non-zero bytes)
        if os.path.exists(self.cache_dir):
            existing_pngs = glob.glob(os.path.join(self.cache_dir, "*.png"))
            if len(existing_pngs) == total_pages and all(os.path.getsize(p) > 0 for p in existing_pngs):
                print(f"Reusing {len(existing_pngs)} existing cached images for {self.filename}...")
                return len(existing_pngs)

            # Otherwise, clean up incomplete/corrupted cache before re-rendering
            shutil.rmtree(self.cache_dir, ignore_errors=True)
            if os.path.exists(self.cache_dir):
                subprocess.run(["rm", "-rf", self.cache_dir], check=False)

        os.makedirs(self.cache_dir, exist_ok=True)
        
        prefix = os.path.join(self.cache_dir, "page")
        print(f"Generating images for {self.filename} in parallel chunks...")

        # 3. Define the start and end pages for each chunk (e.g., 1-20, 21-40)
        chunks = []
        for start in range(1, total_pages + 1, chunk_size):
            end = min(start + chunk_size - 1, total_pages)
            chunks.append((start, end))

        # 4. Worker function for a single chunk
        def process_chunk(start_page: int, end_page: int):
            cmd = [
                "pdftoppm", 
                "-png", 
                "-r", "300", 
                "-f", str(start_page), 
                "-l", str(end_page), 
                self.file_path, 
                prefix
            ]
            subprocess.run(cmd, check=True)

        # 5. Execute chunks in parallel
        # pdftoppm automatically names files with their absolute page number even when chunked
        max_workers = min(len(chunks), (os.cpu_count() or 4))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_chunk, c[0], c[1]) for c in chunks]
            concurrent.futures.wait(futures)
        
        # Return count
        return len(glob.glob(os.path.join(self.cache_dir, "*.png")))

    def _natural_sort_key(self, s):
        """Helper to sort filenames like page-1.png, page-2.png, page-10.png correctly."""
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s)]

    def _clean_hyphenation(self, text: str) -> str:
        """
        Fixes hyphenation split across lines within a single page context.
        Strategy:
        1. If char after break is lowercase -> Join (ge-\\nlegt -> gelegt)
        2. If char after break is Uppercase -> Keep hyphen (Abdul-\\nBaha -> Abdul-Baha)
        """
        def replace_match(match):
            next_char = match.group(1)
            if next_char.islower():
                return next_char # Delete hyphen and newline
            else:
                return f"-{next_char}" # Keep hyphen, delete newline
        
        # Regex explanation:
        # (?<=\w)   : Lookbehind ensures hyphen is attached to a preceding word character
        # -         : The hyphen
        # \s*\n\s* : The newline (and any surrounding whitespace/indentation)
        # (.)       : Capture the very next character
        return re.sub(r'(?<=\w)-\s*\n\s*(.)', replace_match, text)

    def run_ocr(self, config: OcrConfig, progress_callback: Optional[Callable[[int, int], None]] = None):
        """
        Main execution loop.
        1. Reads images.
        2. OCRs them.
        3. Cleans hyphenation.
        4. Formats template.
        5. Writes single .txt file.
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

        # Prevent Tesseract OpenMP thread contention when running multiple workers
        os.environ["OMP_THREAD_LIMIT"] = "1"

        # Pre-calculate page labels sequentially so numbering stays 100% deterministic
        page_tasks = []
        for img_path in image_files:
            filename_only = os.path.basename(img_path)
            page_num_match = re.findall(r'\d+', filename_only)
            if not page_num_match:
                continue
            physical_page = int(page_num_match[-1])

            if config.has_cover_image and physical_page == 1:
                print(f"Skipping cover image {physical_page}")
                continue

            label, illus_counter, real_page_counter = self._get_page_label(
                physical_page, config, illus_counter, real_page_counter
            )
            page_tasks.append((physical_page, label, img_path))

        def _ocr_single_page(task):
            physical_page, label, img_path = task
            # Pass img_path directly so pytesseract doesn't write duplicate temp files
            text = pytesseract.image_to_string(img_path, lang=config.language, timeout=60)
            text = text.replace('\f', '')
            text = self._clean_hyphenation(text)
            template = f"{{{{page|{label}|file={self.filename}|page={physical_page}}}}}"
            return physical_page, f"{template}\n{text}\n"

        results_by_page = {}
        # Cap at 4-6 workers with OMP_THREAD_LIMIT=1 so CPU & external disk never thrash
        max_workers = max(1, min(6, (os.cpu_count() or 4) // 2))
        total_tasks = len(page_tasks)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_page = {executor.submit(_ocr_single_page, t): t[0] for t in page_tasks}
            completed = 0
            for future in concurrent.futures.as_completed(future_to_page):
                phys_page, content = future.result()
                results_by_page[phys_page] = content
                completed += 1
                print(f"[{self.book_name}] OCR completed {completed}/{total_tasks} (Page {phys_page})")
                if progress_callback:
                    progress_callback(completed, total_tasks)

        for physical_page, _, _ in page_tasks:
            full_text_content.append(results_by_page[physical_page])

        # 4. Save to Disk
        with open(self.output_txt_path, "w", encoding="utf-8") as f:
            f.writelines(full_text_content)
        
        print(f"Success! Saved to {self.output_txt_path}")
        return self.output_txt_path

    def extract_text_layer(self, config: OcrConfig, progress_callback: Optional[Callable[[int, int], None]] = None):
        """
        Extracts embedded digital text directly from the PDF via PyMuPDF (fitz),
        skipping image rendering and Tesseract OCR.
        """
        illus_counter = 1
        real_page_counter = 1
        full_text_content = []

        print(f"Extracting embedded text layer for {self.book_name}...")

        with fitz.open(self.file_path) as pdf:
            total_pages = len(pdf)

            for page_idx in range(total_pages):
                physical_page = page_idx + 1  # 1-based physical page number

                if progress_callback:
                    progress_callback(physical_page, total_pages)

                # Skip cover image if configured
                if config.has_cover_image and physical_page == 1:
                    print(f"Skipping cover page {physical_page}")
                    continue

                # A. Calculate Page Label
                label, illus_counter, real_page_counter = self._get_page_label(
                    physical_page, config, illus_counter, real_page_counter
                )

                # B. Extract Text Layer directly
                page = pdf[page_idx]
                text = page.get_text("text")

                # Clean generic garbage (form feed characters) and hyphenation
                text = text.replace('\f', '')
                text = self._clean_hyphenation(text)

                # C. Format Template
                template = f"{{{{page|{label}|file={self.filename}|page={physical_page}}}}}"
                page_content = f"{template}\n{text}\n"
                full_text_content.append(page_content)

        with open(self.output_txt_path, "w", encoding="utf-8") as f:
            f.writelines(full_text_content)

        print(f"Success! Saved text layer to {self.output_txt_path}")
        return self.output_txt_path
    
    def cleanup(self, config: OcrConfig):
        """
        Nukes the temp folder after OCR completes.
        """
        if not os.path.exists(self.cache_dir):
            return

        shutil.rmtree(self.cache_dir, ignore_errors=True)
        if os.path.exists(self.cache_dir):
            subprocess.run(["rm", "-rf", self.cache_dir], check=False)
