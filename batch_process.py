import time
import sys
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm

# Local imports
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document

def process_batch(target_id=None):
    """
    Fetches documents and processes them.
    If target_id is provided, processes ONLY that ID (ignoring status).
    Otherwise, processes all documents with status="PENDING".
    """
    
    with Session(engine) as session:
        # 1. Build Query based on mode
        stmt = select(Document)

        if target_id:
            print(f"--- Processing Single Target ID: {target_id} ---")
            # We filter ONLY by ID here, allowing you to re-process EVALUATED docs
            stmt = stmt.where(Document.id == target_id)
        else:
            print("--- Starting Batch Processor (PENDING items only) ---")
            stmt = stmt.where(Document.status == "PENDING")

        # Execute query
        docs_to_process = session.scalars(stmt).all()
        
        total_count = len(docs_to_process)
        if total_count == 0:
            msg = f"ID {target_id}" if target_id else "PENDING documents"
            print(f"No documents found matching: {msg}")
            return

        print(f"Found {total_count} document(s) to process.")
        print("Press Ctrl+C to pause safely at any time.\n")

        # 2. Iterate with a progress bar
        for doc in tqdm(docs_to_process, unit="file"):
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

                # Commit after every file
                session.commit()

            except KeyboardInterrupt:
                print("\n\nStopping safely... Progress saved.")
                sys.exit(0)
            except Exception as e:
                print(f"\nError on {doc.filename}: {e}")
                doc.status = "SKIPPED_CRASH"
                session.commit()

    print("\n--- Processing Complete ---")

if __name__ == "__main__":
    # Check if a command line argument (ID) was passed
    if len(sys.argv) > 1:
        try:
            # sys.argv[0] is the script name, sys.argv[1] is the first argument
            p_id = int(sys.argv[1])
            process_batch(target_id=p_id)
        except ValueError:
            print("Error: The provided ID must be an integer.")
            print("Usage: python batch_factory.py [ID]")
    else:
        # Default behavior: Process all pending
        process_batch()
