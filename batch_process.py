import time
import sys
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm  # Progress bar library

# Local imports
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document

def process_batch():
    """
    Fetches all PENDING documents and processes them sequentially.
    """
    print("--- Starting Batch Processor ---")
    
    with Session(engine) as session:
        # 1. Get all pending documents
        stm = select(Document).where(Document.status == "PENDING")
        pending_docs = session.scalars(stm).all()
        
        total_count = len(pending_docs)
        if total_count == 0:
            print("No pending documents found! Run the crawler first.")
            return

        print(f"Found {total_count} documents to process.")
        print("Press Ctrl+C to pause safely at any time.\n")

        # 2. Iterate with a progress bar
        # We use tqdm to show a nice progress bar in the terminal
        for doc in tqdm(pending_docs, unit="file"):
            try:
                # A. Extract Images
                images = extract_preview_images(doc.file_path)
                
                if not images:
                    doc.status = "SKIPPED_ERROR"
                    doc.ai_justification = "Could not extract images (corrupt PDF?)"
                    session.commit()
                    continue

                # B. AI Evaluation
                # Small sleep to be nice to the API rate limits (optional)
                time.sleep(1) 
                
                result = evaluate_document(images)
                
                if result:
                    doc.priority_score = result['priority_score']
                    doc.summary = result['summary']
                    doc.language = result['language']
                    doc.ai_justification = result['ai_justification']
                    doc.status = "EVALUATED"
                else:
                    doc.status = "SKIPPED_API_FAIL"
                    doc.ai_justification = "AI returned None (API Error)"

                # Commit after every file so we don't lose progress if crashed
                session.commit()

            except KeyboardInterrupt:
                print("\n\nStopping safely... Progress saved.")
                sys.exit(0)
            except Exception as e:
                print(f"\nError on {doc.filename}: {e}")
                doc.status = "SKIPPED_CRASH"
                session.commit()

    print("\n--- Batch Processing Complete ---")

if __name__ == "__main__":
    process_batch()
