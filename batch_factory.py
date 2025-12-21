import os
import sys
import fitz  # PyMuPDF for page counting
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.database import engine, Document
from src.ocr_engine import OcrEngine, OcrConfig
from src.auto_config import calculate_start_offset

def run_factory():
    print("🏭 Starting Bahai.works Automation Factory...")
    
    with Session(engine) as session:
        # 1. Get High Priority items that are NOT digitized yet
        stm = select(Document).where(
            Document.priority_score >= 8,
            Document.status != "DIGITIZED"
        )
        queue = session.scalars(stm).all()
        
        print(f"Found {len(queue)} high-priority documents to process.")
        
        for doc in queue:
            print(f"\n--- Processing: {doc.filename} ---")
            
            # Step A: Get Page Count
            try:
                pdf = fitz.open(doc.file_path)
                total_pages = len(pdf)
                pdf.close()
            except:
                print("❌ Could not open PDF.")
                continue

            # Step B: Auto-Calibrate Offset
            print("   Triangulating page numbers with Gemini...")
            start_page = calculate_start_offset(doc.file_path, total_pages)
            
            if start_page:
                print(f"   ✅ LOCK: 'Page 1' begins at PDF Page {start_page}")
                
                # Step C: Configure & Run OCR
                # We assume standard Roman Numeral front matter (has_cover=True)
                config = OcrConfig(
                    has_cover_image=True,
                    first_numbered_page_index=start_page,
                    illustration_ranges=[], # Automation cannot guess these yet, safe to leave empty
                    language=doc.language if doc.language in ['eng', 'deu', 'fas'] else 'eng'
                )
                
                engine_worker = OcrEngine(doc.file_path)
                
                # Generate Images
                engine_worker.generate_images()
                
                # Run OCR
                engine_worker.run_ocr(config)
                
                # Cleanup
                engine_worker.cleanup()
                
                # Update DB
                doc.status = "DIGITIZED"
                session.commit()
                print("   🎉 Digitization Complete!")
                
            else:
                print("   ⚠️  Could not determine page offset automatically. Skipping.")
                doc.ai_justification = (doc.ai_justification or "") + "\n[Auto-OCR Failed: Offset Unclear]"
                session.commit()

if __name__ == "__main__":
    run_factory()
