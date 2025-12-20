import hashlib
import sys
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import select

# Local imports
from src.config import SOURCE_DIRECTORIES, VALID_EXTENSIONS
from src.database import engine, Document

def calculate_file_hash(file_path: Path) -> str:
    """
    Reads file in chunks to calculate MD5 hash efficiently.
    Prevents loading massive PDFs entirely into memory.
    """
    hasher = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            # Read in 64kb chunks
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as e:
        print(f"Error hashing {file_path}: {e}")
        return None

def scan_directories():
    """
    Main loop:
    1. Traverses source directories.
    2. Hashes valid files.
    3. Commits new findings to DB.
    """
    print("--- Starting File Crawl ---")
    
    with Session(engine) as session:
        new_files_count = 0
        skipped_count = 0

        for root_dir in SOURCE_DIRECTORIES:
            if not root_dir.exists():
                print(f"Warning: Directory not found: {root_dir}")
                continue

            print(f"Scanning: {root_dir}...")
            
            # Recursive walk for files
            for file_path in root_dir.rglob("*"):
                # Filter by extension (case-insensitive)
                if file_path.suffix.lower() not in VALID_EXTENSIONS:
                    continue

                # Calculate Hash
                file_hash = calculate_file_hash(file_path)
                if not file_hash:
                    continue

                # Check if hash exists in DB
                existing_doc = session.execute(
                    select(Document).where(Document.file_hash == file_hash)
                ).scalar_one_or_none()

                if existing_doc:
                    # Optional: Update path if file moved (logic omitted for simplicity)
                    skipped_count += 1
                    continue

                # Create new record
                new_doc = Document(
                    file_path=str(file_path),
                    filename=file_path.name,
                    file_hash=file_hash,
                    status="PENDING"  # Ready for AI evaluation
                )
                session.add(new_doc)
                new_files_count += 1
                
                # Commit every 10 files to keep DB fresh during long scans
                if new_files_count % 10 == 0:
                    session.commit()
                    sys.stdout.write(f"\rFound {new_files_count} new files...")
                    sys.stdout.flush()

        session.commit()
        print(f"\n--- Scan Complete ---")
        print(f"New Files Added: {new_files_count}")
        print(f"Duplicates/Existing Skipped: {skipped_count}")

if __name__ == "__main__":
    scan_directories()
