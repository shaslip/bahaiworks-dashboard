import os
import sys
import fitz  # PyMuPDF
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.ocr_engine import OcrEngine, OcrConfig
from src.auto_config import calculate_start_offset

def run_factory():
    print("🏭 Starting Bahai.works Automation Factory...")
    print("   Scanning database for High Priority (>8) items pending digitization...")
    
    with Session(engine) as session:
        # Get High Priority items that are NOT digitized
        stm = select(Document).where(
            Document.priority_score >= 8,
            Document.status != "DIGITIZED"
        )
        queue = session.scalars(stm).all()
        
        if not queue:
            print("   No matching documents found.")
            return

        print(f"   Found {len(queue)} documents to process.")
        
        # INTERACTIVE FLAG: Starts true, turns false if you type 'a'
        ask_permission = True
        
        for i, doc in enumerate(queue, 1):
            print(f"\n[{i}/{len(queue)}] Processing: {doc.filename}")
            
            # --- INTERACTIVE PROMPT ---
            if ask_permission:
                while True:
                    user_input = input("   Would you like to continue? [y/n/a]: ").strip().lower()
                    
                    if user_input in ['n', 'no', 'q', 'quit']:
                        print("   🛑 Stopping factory.")
                        return
                    
                    elif user_input in ['a', 'all']:
                        print("   🚀 fast-forward enabled. Processing all remaining files...")
                        ask_permission = False
                        break
                    
                    elif user_input in ['y', 'yes', '']:
                        break  # Just continue to this file
            # --------------------------

            # 1. Validation check
            if not os.path.exists(doc.file_path):
                print("   ❌ File not found on disk. Skipping.")
                continue

            # 2. Get Total Pages
            try:
                with fitz.open(doc.file_path) as pdf:
                    total_pages = len(pdf)
            except Exception as e:
                print(f"   ❌ Corrupt PDF: {e}")
                continue

            # 3. Auto-Calibrate (The "Detective")
            print("   🔍 Triangulating page offset...")
            start_page = calculate_start_offset(doc.file_path, total_pages)
            
            if start_page:
                print(f"   ✅ LOCK: 'Page 1' starts at PDF Page {start_page}")
                
                # 4. Configure Job
                lang_map = {'German': 'deu', 'Persian': 'fas', 'French': 'fra'}
                ocr_lang = 'eng'
                for k, v in lang_map.items():
                    if doc.language and k in doc.language:
                        ocr_lang = v
                        break
                
                config = OcrConfig(
                    has_cover_image=True,
                    first_numbered_page_index=start_page,
                    illustration_ranges=[], 
                    language=ocr_lang
                )
                
                # 5. Execute OCR (The "Worker")
                try:
                    worker = OcrEngine(doc.file_path)
                    
                    print("   📸 Generating page images...")
                    worker.generate_images()
                    
                    print(f"   📖 Reading text ({ocr_lang})...")
                    worker.run_ocr(config)
                    
                    worker.cleanup()
                    
                    doc.status = "DIGITIZED"
                    session.commit()
                    print("   🎉 Success! DB updated.")
                    
                except Exception as e:
                    print(f"   ❌ OCR Failure: {e}")
                    worker.cleanup()
            
            else:
                print("   ⚠️  Calibration Failed: Offsets did not agree (need 2/3). Skipping.")
                doc.ai_justification = (doc.ai_justification or "") + "\n[Auto-OCR Failed: Calibration Mismatch]"
                session.commit()

    print("\n🏭 Factory shutdown. All jobs complete.")

if __name__ == "__main__":
    run_factory()
