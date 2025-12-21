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
    print("--- Bahai.works Automation Factory ---")
    
    with Session(engine) as session:
        # 1. Determine which documents to fetch
        if target_id:
            # STRICT MODE: Only fetch the specific ID, ignore status/priority
            print(f"Target Mode: Processing single document ID {target_id}...")
            stmt = select(Document).where(Document.id == target_id)
        else:
            # BATCH MODE: Fetch all pending documents
            print("Batch Mode: Scanning for PENDING documents...")
            stmt = select(Document).where(Document.status == "PENDING")

        # 2. Execute the query
        docs = session.scalars(stmt).all()
        total_count = len(docs)

        if total_count == 0:
            print(f"No documents found. (Target ID: {target_id})")
            return

        print(f"Found {total_count} document(s).")
        
        # 3. Process loop
        for doc in tqdm(docs, unit="file"):
            try:
                # A. Extract Images
                images = extract_preview_images(doc.file_path)
                
                if not images:
                    doc.status = "SKIPPED_ERROR"
                    doc.ai_justification = "Could not extract images"
                    session.commit()
                    continue

                # B. AI Evaluation
                time.sleep(1) # Rate limit pause
                result = evaluate_document(images)
                
                if result:
                    doc.priority_score = result['priority_score']
                    doc.summary = result['summary']
                    doc.language = result['language']
                    doc.ai_justification = result['ai_justification']
                    doc.status = "EVALUATED"
                else:
                    doc.status = "SKIPPED_API_FAIL"
                    doc.ai_justification = "AI returned None"

                session.commit()

            except KeyboardInterrupt:
                print("\nStopping safely.")
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
            p_id = int(sys.argv[1])
            process_batch(target_id=p_id)
        except ValueError:
            print("Error: ID must be an integer.")
    else:
        # No argument -> Run standard batch
        process_batch()
