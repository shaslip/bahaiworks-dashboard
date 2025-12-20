import os
from pathlib import Path

# --- Project Paths ---
# The absolute path to the 'dashboard' folder
BASE_DIR = Path(__file__).resolve().parent.parent

# The path where the SQLite database will be stored
DB_PATH = BASE_DIR / "bahai_works.db"

# --- Source Directories ---
# The crawler will look recursively into these folders
SOURCE_DIRECTORIES = [
    Path("/home/sarah/Desktop/Projects/Bahai.works/German/1.Incoming/"),
    Path("/home/sarah/Desktop/Projects/Bahai.works/English/1.Donated/"),
    Path("/home/sarah/Desktop/Projects/Bahai.works/English/3.Miscbahai/1.incoming/"),
]

# --- File Filtering ---
# Extensions we care about. 
# We ignore .txt or .doc for now if you only want scanned sources (PDF/Images).
VALID_EXTENSIONS = {".pdf"}
