import time
import sys
from sqlalchemy import select
from sqlalchemy.orm import Session
from tqdm import tqdm  # Progress bar library

# Local imports
from src.database import engine, Document
from src.processor import extract_preview_images
from src.evaluator import evaluate_document

def process_batch(target_id=None):
    """
    Fetches documents. If target_id is set, processes ONLY that document.
    Otherwise, processes all PENDING documents.
    """
    
    with Session(engine) as session:
        # 1. Determine Query
        if target_id:
            print(f"--- SINGLE ITEM MODE: Processing ID {target_id} ---")
            # Strict filter: Only this ID, ignore status
            stm = select(Document).where(Document.id == target_id)
        else:
            print("--- BATCH MODE: Processing PENDING items ---")
            stm = select(Document).where(Document.status == "PENDING")

        # 2. Execute Query
        docs_to_process = session.scalars(stm).all()
        
        total_count = len(docs_to_process)
        if total_count == 0:
            print(f"No documents found matching criteria (ID: {target_id}).")
            return

        print(f"Found {total_count} document(s) to process.")
        print("Press Ctrl+C to pause safely at any time.\n")

        # 3. Process Loop
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

                # Commit after every file so we don't lose progress if crashed
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
    # Check for command line argument
    if len(sys.argv) > 1:
        try:
            # sys.argv[0] is script name, [1] is first arg
            p_id = int(sys.argv[1])
            process_batch(target_id=p_id)
        except ValueError:
            print("Error: The provided ID must be an integer.")
    else:
        # Default: Run batch
        process_batch()
