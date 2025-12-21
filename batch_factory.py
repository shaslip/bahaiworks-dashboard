import os
import sys
import re
import fitz  # PyMuPDF
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.ocr_engine import OcrEngine, OcrConfig
from src.auto_config import calculate_start_offset
from src.processor import merge_pdf_pair

def run_factory():
    print("🏭 Starting Bahai.works Automation Factory...")
    print("    Scanning database for High Priority (>8) items pending digitization...")
    
    with Session(engine) as session:
        # Get High Priority items that are NOT digitized
        stm = select(Document).where(
            Document.priority_score >= 8,
            Document.status.notin_(["DIGITIZED", "COMPLETED"])
        )
        queue = session.scalars(stm).all()
        
        if not queue:
            print("    No matching documents found.")
            return

        print(f"    Found {len(queue)} documents to process.")
        
        # INTERACTIVE FLAG: Starts true, turns false if you type 'a'
        ask_permission = True
        
        # Regex for identifying split parts
        # Captures: Group 1 (Base Name), Group 2 (Suffix Type: Cover or Inhalt gesamt)
        split_pattern = re.compile(r"^(.*?)\s*-\s*(Cover|Inhalt gesamt)\.pdf$", re.IGNORECASE)

        for i, doc in enumerate(queue, 1):
            # Refresh doc status in case it was modified by a previous merge in this loop
            session.refresh(doc)
            
            if doc.status == "DIGITIZED":
                print(f"\n[{i}/{len(queue)}] Skipping {doc.filename} (Already processed via merge partner).")
                continue

            print(f"\n[{i}/{len(queue)}] Processing: {doc.filename}")
            
            skip_file = False

            # --- INTERACTIVE PROMPT ---
            if ask_permission:
                while True:
                    user_input = input("    Would you like to continue? [y/n/a/s]: ").strip().lower()
                    
                    if user_input in ['n', 'no', 'q', 'quit']:
                        print("    🛑 Stopping factory.")
                        return
                    
                    elif user_input == 's':
                        skip_file = True
                        break
                        
                    elif user_input in ['a', 'all']:
                        print("    🚀 fast-forward enabled. Processing all remaining files...")
                        ask_permission = False
                        break
                    
                    elif user_input in ['y', 'yes', '']:
                        break  # Just continue to this file
            
            if skip_file:
                print("    ⏩ Skipping.")
                continue
            # --------------------------

            # 1. Validation check
            if not os.path.exists(doc.file_path):
                print("    ❌ File not found on disk. Skipping.")
                continue

            # --- MERGE LOGIC START ---
            current_path = doc.file_path
            partner_doc = None
            
            match = split_pattern.match(doc.filename)
            if match:
                base_name = match.group(1)
                current_type = match.group(2).lower() # 'cover' or 'inhalt gesamt'
                
                # Determine partner suffix
                partner_suffix = "Inhalt gesamt" if "cover" in current_type else "Cover"
                partner_filename = f"{base_name} - {partner_suffix}.pdf"
                
                # Look for partner in DB
                print(f"    🧩 Split file detected. Looking for partner: {partner_filename}...")
                partner_doc = session.execute(
                    select(Document).where(Document.filename == partner_filename)
                ).scalar_one_or_none()
                
                if partner_doc:
                    # Determine paths
                    cover_path = doc.file_path if "cover" in current_type else partner_doc.file_path
                    content_path = partner_doc.file_path if "cover" in current_type else doc.file_path
                    
                    # Define new clean output path (remove suffix)
                    clean_filename = f"{base_name}.pdf"
                    clean_path = os.path.join(os.path.dirname(doc.file_path), clean_filename)
                    
                    print(f"    🔗 Merging into: {clean_filename}")
                    if merge_pdf_pair(cover_path, content_path, clean_path):
                        current_path = clean_path # Override path for processing
                    else:
                        print("    ⚠️ Merge failed. Skipping this pair.")
                        continue
                else:
                    print("    ⚠️ Partner file not found in DB. Skipping.")
                    # Optional: Mark as MISSING_PART here if desired
                    continue
            # --- MERGE LOGIC END ---

            # 2. Get Total Pages (using the potentially merged file)
            try:
                with fitz.open(current_path) as pdf:
                    total_pages = len(pdf)
            except Exception as e:
                print(f"    ❌ Corrupt PDF: {e}")
                continue

            # 3. Auto-Calibrate (The "Detective")
            print("    🔍 Triangulating page offset...")
            start_page = calculate_start_offset(current_path, total_pages)
            
            if start_page:
                print(f"    ✅ LOCK: 'Page 1' starts at PDF Page {start_page}")
                
                # 4. Configure Job
                lang_map = {'German': 'deu', 'Persian': 'fas', 'French': 'fra', 'Esperanto': 'epo'}
                ocr_lang = 'eng'
                for k, v in lang_map.items():
                    if doc.language and k in doc.language:
                        ocr_lang = v
                        break
                
                use_cover = (start_page > 1)
                
                config = OcrConfig(
                    has_cover_image=use_cover,
                    first_numbered_page_index=start_page,
                    illustration_ranges=[], 
                    language=ocr_lang
                )
                
                # 5. Execute OCR (The "Worker")
                try:
                    worker = OcrEngine(current_path)
                    
                    print("    📸 Generating page images...")
                    worker.generate_images()
                    
                    print(f"    📖 Reading text ({ocr_lang})...")
                    worker.run_ocr(config)
                    
                    worker.cleanup()
                    
                    # Update Status
                    doc.status = "DIGITIZED"
                    if partner_doc:
                        partner_doc.status = "DIGITIZED"
                        print(f"    🤝 Partner '{partner_doc.filename}' also marked DIGITIZED.")
                        
                    session.commit()
                    print("    🎉 Success! DB updated.")
                    
                except Exception as e:
                    print(f"    ❌ OCR Failure: {e}")
                    worker.cleanup()
            
            else:
                print("    ⚠️  Calibration Failed: Offsets did not agree (need 2/3). Skipping.")
                doc.ai_justification = (doc.ai_justification or "") + "\n[Auto-OCR Failed: Calibration Mismatch]"
                session.commit()

    print("\n🏭 Factory shutdown. All jobs complete.")

if __name__ == "__main__":
    run_factory()
